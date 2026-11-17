# QCopilots-WebSearch

[English](README.md)

使用 C++20 和 Qt 6.12 实现的独立网页搜索服务，通过自部署 SearXNG 搜索公网，提供网页阅读、页内查找、REST、MCP HTTP 和 MCP stdio。QGIS 通过 QGIS 仓库中的独立 Python MCP 适配插件 `python/plugins/qcopilots_mcp_server_web_search` 接入，接入步骤见[团队部署](docs/deployment.md#qgis)。同一原生服务也可用于其他项目。

CI 构建并测试 Windows 和 Ubuntu 安装包。推送 `vMAJOR.MINOR.PATCH` 标签后，流水线执行完整自动检查，核对安装包和构建来源，然后发布 GitHub Release。Windows CI 使用 Windows Server 2022。平台覆盖范围与必需检查详见[发布流程](docs/release.md)。

## 能力与边界

| 工具 | 行为 |
| --- | --- |
| `web_search` | 返回可归属来源的搜索结果，强制执行域名及子域过滤 |
| `web_open` | 将公网 HTML 或文本保存为调用身份专属的缓存快照，按行分页 |
| `web_find` | 对快照执行字面量查找，默认使用 Unicode 忽略大小写匹配 |
| `web_search_status` | 读取服务与最近观察到的上游状态，不触发公网搜索 |

首版不包含自主索引、付费后端、PDF 提取、浏览器 JavaScript 执行或模型生成答案。自部署不设置商业调用配额，但上游引擎仍可能限流、要求验证码或临时不可用。公网表现按尽力而为报告。

默认允许五个并发业务请求，满额立即返回 `busy`。搜索默认截止时间为 15 秒，网页读取为 20 秒。搜索缓存默认保留 5 分钟，页面快照保留 30 分钟。缓存身份、输出预算和错误码由版本化契约约束。

## 构建与运行

Windows 需先具备 VS2022 C++ 工作负载、Windows SDK、CMake 3.30+、Ninja、Git、原生 Windows Perl 和 Python 3.12+。引导阶段下载固定版本 Qt，默认安装根目录为 `C:/Softwares/Qt`，不安装 Visual Studio 或 Perl。Ubuntu 引导阶段通过 apt 准备 GCC 等工具和系统依赖，再下载 Qt。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/BuildPipeline.ps1 --Profile windows-x64 --Stages bootstrap,doctor,configure,build,test,install
```

```bash
bash scripts/BuildPipeline.sh --Profile ubuntu-x64 --Stages bootstrap,doctor,configure,build,test,install
```

复制 `config/BuildPipeline.json` 为机器专属的 `*.local.json` 后，通过 `--ConfigPath` 指定。`proxy` 可设为 `http://127.0.0.1:7897`。下载代理只影响构建依赖，运行时网页请求不会继承代理。`--CheckOnly` 仅检查环境，`--DryRun` 仅显示计划命令。

`publish` 阶段只生成本地安装包。Windows MSI 另需官方 WiX 7.0.1、同版本 UI 扩展和 .NET 10，请阅读其官方包的维护费条件。正式 GitHub Release 由标签触发的 CI 自动发布，包含两平台安装包、SBOM、匹配源码和校验和。

```text
qcopilots-websearch --InitializeConfig --ConfigPath 私有目录/service.json
qcopilots-websearch --CheckConfig --ConfigPath 私有目录/service.json
qcopilots-websearch --ConfigPath 私有目录/service.json
```

初始化会生成随机客户端令牌，并保留已存在的配置。设置 `provider.url` 指向启用 JSON 输出的私有 SearXNG `/search` 端点。默认业务地址为 `127.0.0.1:8088`，健康与指标地址为 `127.0.0.1:8089`，两者均需认证。远程访问通过 HTTPS 反向代理，并配置精确 Host 和 Origin 白名单。

REST 提供 `POST /v1/search`、`/v1/open`、`/v1/find` 和 `GET /v1/status`，使用 Bearer 认证。MCP HTTP 地址为 `/mcp`，协议版本为 `2025-11-25` 或 `2025-06-18`。添加 `--Stdio` 可通过标准输入输出连接，使用独立临时缓存且不监听 HTTP。

网页内容属于不可信外部资料。客户端应将返回内容用于引用与分析。公共抓取会检查真实连接地址及每次重定向，阻止内网和云元数据目标，不继承系统代理。

## 目录结构

- `app`、`include/qws`、`src`：原生应用与可复用服务组件
- `contracts/v1`：规范工具模式与工具元数据
- `config`：构建与运行配置示例、配置模式及固定依赖哈希
- `scripts`、`cmake`：共享的构建与分发工具
- `tests`：使用临时状态和动态端口的确定性测试
- `docs`：架构、接口、构建、部署与运维文档

原生仓库采用 Apache-2.0，第三方依赖保留各自许可。Qt 动态链接，正式发行附匹配源码及共享库替换说明。QGIS 适配代码留在 QGIS 仓库中并遵循 GPL 许可。

进一步阅读[开源调研](docs/research.md)、[运行架构](docs/architecture.md)、[接口说明](docs/api.md)、
[构建说明](docs/build.md)、[团队部署](docs/deployment.md)、
[运维指南](docs/operations.md)、[发布流程](docs/release.md)及[许可说明](docs/licensing.md)。
