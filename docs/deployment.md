<a name="team-deployment"></a>

# 团队部署

本部署示例适用于 10 至 50 人的团队，默认允许同时处理五项业务操作。搜索结果取决于上游服务的可用性。公开搜索引擎可能只返回部分结果、要求验证码或限制请求频率。服务遵守这些限制。

<a name="installed-versions-and-ports"></a>

## 安装版本与端口

MSI 和 DEB 安装包会为每个项目版本注册独立服务。新版本首次安装时会创建自己的部署配置、客户端令牌、管理令牌、设置、搜索源密钥和缓存。运行配置和令牌位于该版本的 `bin/data` 目录，设置、搜索源密钥和缓存位于 `bin/data/state`。将同一版本重新安装到原目录时，会继续使用该版本的数据。先前安装的版本继续使用各自的目录和配置。

安装程序优先选择业务、控制和管理监听器的默认回环端口，依次为 8088、8089 和 8091。安装程序会跳过已被占用的端口，以及其他已安装版本配置中记录的端口，即使对应服务当前处于停止状态。选定的端口会写入该版本的运行配置，服务重启时继续使用这些端口。REST 和 MCP 使用 `business.port`，健康检查和指标使用 `control.port`，设置页使用 `admin.port`。连接时请使用同一版本的令牌。

<a name="ubuntu-host"></a>

## Ubuntu 主机

使用 apt 安装发布版 DEB，将 `version` 设为安装包文件名中的版本号。每个安装包都会创建该版本专用的非特权服务账号和 systemd 单元。DEB 要求 `/opt` 归 `root` 所有，且仅允许所有者写入。

```bash
version=0.2.1
package=qcopilots-websearch-$version
sudo apt install "./${package}_${version}_amd64.deb"
sudo systemctl status "$package.service"
```

systemd 服务读取 `/opt/qcopilots-websearch-<version>/bin/data/runtime.json`。管理员可以编辑该文件，对应的服务账号可以读取。该账号拥有同目录下的 `client.token` 和 `admin.token`，两者的权限均为 0600。

运行目标版本的可执行文件，生成该版本的一次性管理链接。

```bash
sudo "/opt/$package/bin/qcopilots-websearch" --SettingsUrl
```

在服务所在主机的浏览器中打开链接。若要从远程浏览器访问，可通过 SSH 隧道连接到对应的回环地址，或配置专用 HTTPS 代理，并设置管理端的 `Host`、`Origin` 和公开源地址。下文的 Caddy 部署示例对外提供业务接口。

将 `deploy/team` 复制到管理员控制的部署目录。将 `.env.example` 复制为 `.env`，设置实际的 DNS 主机名和 ACME 联系方式。用服务的 `--GenerateToken` 命令生成密码学安全的随机值，作为 `SEARXNG_SECRET`。直接将其写入权限为 0600 的私有环境文件，避免输出到终端或 CI 日志。

在复制后的 `Caddyfile` 中，将反向代理的上游设为 `127.0.0.1:<business.port>`，其中端口取自要对外提供服务的版本的运行配置。

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d
```

仅 Caddy 使用主机网络，因此它能访问服务的回环监听器。SearXNG 映射至 `127.0.0.1:8888`，Valkey 只在 Compose 内部网络可访问。Caddy 对外提供 HTTPS API，它自身的存活检查使用回环端口 8090。自动申请证书需要公网 DNS 和入站 80、443 端口。服务自身的控制端点保持在本地。

在设置页选择 SearXNG，将端点设为 `http://127.0.0.1:8888/search`。在 `business.allowed_hosts` 中加入实际的外部主机名及所用端口，例如 `search.example.org` 和 `search.example.org:443`。使用 QCopilots WebUI 时，将准确的 `Origin` 值 `https://chat.vastarmorai.com` 加入 `business.origins`。保留回环绑定，以及本地客户端所需的准确主机和端口。源地址末尾省略斜杠。

为每个集成方或团队成员分配独立的客户端身份及令牌文件。原生后端凭据与 QGIS 本地 MCP 凭据分开管理。使用目标版本的服务账号检查配置，再重启该版本。

```bash
account=$(systemctl show --property=User --value "$package.service")
sudo -u "$account" "/opt/$package/bin/qcopilots-websearch" --CheckConfig
sudo systemctl restart "$package.service"
```

Caddy 原样转发客户端的 `Authorization`、`Host` 和 `Origin`，由原生服务验证客户端凭据。保留上游请求的默认取消行为，避免添加会改变客户端断开时取消行为的 `flush_interval -1`。原生服务会拒绝配置中未允许的 `Origin` 和 `Host`。原生服务监听器、Caddy 健康检查监听器和 SearXNG 均应保持在外部主机无法直接访问的范围内。

本私有后端模板关闭了 SearXNG 自带的限流器。SearXNG 作为内部服务运行。如需单独对外开放，应为其设计身份验证、限流和代理地址策略。`compose.yml` 固定了容器镜像摘要。更新镜像时应重新执行验收。

## Windows

MSI 安装 `QCopilotsWebSearch-<version>` 系统服务，运行账号为虚拟账号 `NT SERVICE\QCopilotsWebSearch-<version>`。默认程序目录是 `%ProgramFiles%\QCopilots-WebSearch-<version>`。服务读取该目录中的 `bin\data\runtime.json`。

如需改用其他目录，请在以管理员身份打开的命令提示符中设置 `INSTALL_ROOT`。程序及其 `bin\data` 目录均位于指定位置。请为不同版本选择各自的安装目录。

```cmd
msiexec.exe /i "C:\Downloads\QCopilots-WebSearch-0.2.1-win64.msi" INSTALL_ROOT="C:\Program Files\QCopilots-WebSearch-Custom-0.2.1"
```

Windows 使用 Schannel。

本机交互式用户可以读取 `admin.token`，用于登录设置页并管理搜索设置。运行配置、客户端令牌和搜索源密钥各自受文件 ACL 保护。在目标版本安装目录的 `bin` 下运行 `qcopilots-websearch.exe --SettingsUrl`，即可为正在运行的服务生成一次性链接。

请以管理员身份运行 MSI，并阅读安装程序显示的 Apache 许可条款和 Microsoft 运行库条款。安装包的签名状态以发布说明为准，未特别说明时为未签名。在 Windows 的“已安装的应用”中按版本选择要卸载的 MSI。卸载会移除该版本的程序文件和服务，并保留配置和数据。

将便携 ZIP 解压到私有可写目录，每个便携实例使用独立的解压目录。在解压目录运行 `.\bin\qcopilots-websearch.exe`，即可在前台启动服务。首次启动会创建 `bin\data\runtime.json`，并生成客户端令牌和管理令牌。服务运行期间，从另一个终端执行 `.\bin\qcopilots-websearch.exe --SettingsUrl`，打开该实例的设置页。

用 `--ConfigPath FILE` 指定其他配置文件。要初始化自定义路径，请使用 `--InitializeConfig --ConfigPath FILE`。可加上 `--SelectAvailablePorts` 为新配置选择可用的回环端口。新配置默认使用自动搜索，因此可以按需接入私有 SearXNG 实例。

<a name="manage-client-tokens"></a>

## 管理客户端令牌

CLI 可以为现有运行配置生成新令牌，也可以登记已有令牌。两条命令都会在标准错误输出中请求确认，需要输入 `y` 或 `yes`。设置令牌时，命令从标准输入读取令牌，并在交互式终端中隐藏输入。请在同一行输入 32 至 8192 个可见 ASCII 字符，再在下一行确认。生成令牌时，命令会在保存配置后将新令牌写入标准输出。

```text
qcopilots-websearch --ConfigPath FILE --GenerateToken [--ClientId ID]
qcopilots-websearch --ConfigPath FILE --SetToken [--ClientId ID]
```

默认客户端 ID 为 `local`。为不同用户或集成方分配令牌时，请分别指定客户端 ID。对于新 ID，命令会添加条目。对于已有 ID，命令会替换凭据并保留其缓存身份。更新后的条目只保存 `token_sha256`，已有条目仍可使用 `token_env` 或 `token_file`。服务启动时读取凭据，重启后变更生效，被替换的旧令牌随即失效。

将令牌原文存入私有凭据存储，并通过安全渠道交给客户端。配置中的摘要不能用于连接服务或恢复令牌。单独运行 `--GenerateToken` 时，命令只生成并输出令牌。

对于 DEB 安装，以管理员身份生成凭据，再用该版本的服务账号检查配置并重启服务。

```bash
version=0.2.1
package=qcopilots-websearch-$version
executable="/opt/$package/bin/qcopilots-websearch"
config_path="/opt/$package/bin/data/runtime.json"
account=$(systemctl show --property=User --value "$package.service")
sudo "$executable" --ConfigPath "$config_path" --GenerateToken --ClientId team-alice
sudo -u "$account" "$executable" --ConfigPath "$config_path" --CheckConfig
sudo systemctl restart "$package.service"
```

对于 MSI 安装，将 `installRoot` 设为安装程序实际使用的目录，然后在以管理员身份打开的 PowerShell 中运行以下命令。便携版请指定其运行配置路径，并省略 `--ServiceName`。

```powershell
$version = '0.2.1'
$serviceName = "QCopilotsWebSearch-$version"
$installRoot = Join-Path $env:ProgramFiles "QCopilots-WebSearch-$version"
$executable = Join-Path $installRoot 'bin\qcopilots-websearch.exe'
$configPath = Join-Path $installRoot 'bin\data\runtime.json'
& $executable --ConfigPath $configPath --ServiceName $serviceName --GenerateToken --ClientId team-alice
& $executable --ConfigPath $configPath --ServiceName $serviceName --CheckConfig
Restart-Service -Name $serviceName
```

<a name="connect-remote-clients"></a>

## 连接远程客户端

使用目标版本业务监听器对应的 HTTPS 端点。在 `business.allowed_hosts` 中加入该端点准确的主机和端口。客户端从浏览器直接连接时，还要将页面的准确源地址加入 `business.origins`。HTTPS 端点负责将客户端的 Bearer 凭据转发给 QCopilots-WebSearch。

在客户端的 MCP 服务器设置中输入 `https://search.example.org/mcp`，并将示例主机名替换为服务的实际主机名。启用 **Authorization**，在 **Bearer** 字段中粘贴令牌。确认客户端可列出四个工具，并能调用 `web_search_status`。使用 [QGIS 适配器](#qgis) 时，将 `backend_url` 设为 `https://search.example.org` 这样的 HTTPS 源地址，并按既有方式配置令牌来源。

<a name="runtime-trust-and-proxies"></a>

## 运行时证书信任与代理

运行时 HTTPS 使用[架构说明](architecture.md)中列出的信任来源。请在 HTTPS 端点配置完整的服务器证书链。Windows 可通过 AIA 获取中间证书，Linux 则使用端点提供的证书链和本机信任库。两种平台均省略在线 CRL 和 OCSP 查询。运行时请求直接连接目标地址，请配置可直接访问的 SearXNG 端点。构建配置中的代理仅用于下载依赖。

## QGIS

从 QGIS 分支安装独立插件 `qcopilots_mcp_server_web_search`，其源码位于 `python/plugins/qcopilots_mcp_server_web_search`。插件沿用现有的 QCopilots Manager 生命周期管理和本地 MCP HTTP 传输，连接到已运行的原生后端。原生后端进程由其部署方式管理。原有七项服务的启动偏好继续生效。

<a name="give-qgis-its-own-credential"></a>

### 为 QGIS 分配独立凭据

适配器以普通 REST 客户端身份向原生服务进行身份验证。请为每位 QGIS 用户分配独立的客户端 ID，使页面快照和缓存条目仅对该用户可见。

1. 使用 `qcopilots-websearch --ConfigPath FILE --GenerateToken --ClientId qgis-alice` 生成并登记令牌。根据[各平台的令牌管理步骤](#manage-client-tokens)检查配置并重启服务。
2. 将令牌存放在只有该用户可以读取的文件中，或放入其用户环境变量 `QCOPILOTS_WEBSEARCH_BACKEND_TOKEN`。在 Linux 和 macOS 上，适配器会拒绝其他用户可读取的令牌文件。

用 `--InitializeConfig` 创建的单用户部署已生成 `client.token`。如果服务仍在使用该令牌，且本机的 QGIS 能读取该文件，可以将适配器的 `auth.token_file` 指向该文件的绝对路径。

<a name="adapter-settings"></a>

### 配置适配器

将适配器的配置示例复制到私有目录，再通过 `QCOPILOTS_WEBSEARCH_CONFIG` 指定路径。也可直接将其保存为 `<QCOPILOTS_HOME>/web_search/config.json`。请使用专用的 `token_env` 或 `token_file`。已有的 `formal_restricted` 网络允许列表必须明确允许后端源地址。容器继续按既有规则验证回环地址。

| 适配器设置 | 对应的服务端配置 |
| --- | --- |
| `backend_url` | 本地地址使用准确的 `http://127.0.0.1:<business.port>`，并将该主机和端口加入 `business.allowed_hosts`。明文 HTTP 只接受回环 IP 字面量，因此会拒绝 `localhost`。服务绑定 `127.0.0.1`，所以也会拒绝 `[::1]`。远程后端使用不带路径的 HTTPS 源地址，并启用证书验证。 |
| `timeout_seconds` | 取 `limits.search_timeout_seconds` 和 `limits.open_timeout_seconds` 中的较大值，再增加约五秒。客户端默认值为 25 秒，两项服务端的默认超时分别为 15 和 20 秒。客户端超时过短会在服务仍处理请求时返回 `timeout`，重试会重新发起页面读取。 |
| `max_concurrency` | 不超过 `limits.max_concurrent`，该服务端并发上限由所有客户端共用。 |
| `max_response_bytes` | 至少 262144。工具载荷上限为 240 KiB，默认的 1 MiB 可容纳外围响应结构。 |

可通过单独的 `settings` 对象将 `url` 设为管理端源地址，并配置 `admin_token_file` 或 `admin_token_env`。管理凭据独立于后端的业务凭据。Web Search 卡片菜单在管理凭据可用时打开一次性登录链接，否则打开手动登录页。

<a name="start-and-troubleshoot"></a>

### 启动与故障排查

在 Manager 中启动“QCopilots MCP Server Web Search”。默认的本地 MCP URL 为 `http://127.0.0.1:48218/mcp`。使用外部浏览器测试时，还需设置 `QCOPILOTS_EXTERNAL_BROWSER_ACCESS=ON`，配置 WebUI 的 `Origin`，并使用 Manager 的本地 Bearer 凭据。请将原生后端令牌留在后端连接配置中。

关闭“Search without asking”后，QGIS 会在当前会话首次搜索或打开新 URL 之前询问用户。用户拒绝后，QGIS 会等待 120 秒再询问。读取已缓存的页面、执行页内查找或查询状态时可以直接调用。

后端离线时，适配器仍可被发现。初始化、工具列表和本地健康检查独立于公网搜索。调用失败时会返回明确的后端错误。后端重新可用后，调用会恢复。

| 适配器错误 | 常见原因 |
| --- | --- |
| `forbidden` | `backend_url` 的主机和端口未加入 `business.allowed_hosts`，或文件系统策略拒绝访问适配器配置。 |
| `unauthorized` | 适配器与服务配置中的令牌不一致，或服务在新增客户端后还未重启。 |
| `backend_unavailable` | 服务未运行、使用了其他端口，或返回了不符合预期的响应。 |
| `invalid_configuration` | 适配器配置或令牌来源无效。 |
| `timeout` | `timeout_seconds` 小于服务端超时，或上游响应缓慢。 |
