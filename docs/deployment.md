<a name="team-deployment"></a>

# 团队部署

本部署示例适用于 10 至 50 人的团队，默认允许同时处理五项业务操作。搜索结果取决于上游服务的可用性。公开搜索引擎可能只返回部分结果、要求验证码或限制请求频率。服务遵守这些限制。

<a name="installed-versions-and-ports"></a>

## 安装版本与端口

MSI 和 DEB 安装包会为每个项目版本注册独立服务。新版本首次安装时会创建自己的部署配置、客户端令牌、管理令牌、设置、搜索源密钥和缓存。运行配置和令牌位于该版本的 `bin/data` 目录，设置、搜索源密钥和缓存位于 `bin/data/state`。将同一版本重新安装到原目录时，会继续使用该版本的数据。先前安装的版本继续使用各自的目录和配置。

安装程序优先选择业务、控制和管理监听器的默认回环端口，依次为 3181、3182 和 3183。安装程序会跳过已被占用的端口，以及其他已安装版本配置中记录的端口，即使对应服务当前处于停止状态。选定的端口会写入该版本的运行配置，服务重启时继续使用这些端口。REST 和 MCP 使用 `business.port`，健康检查和指标使用 `control.port`，设置页使用 `admin.port`。连接时请使用同一版本的令牌。

<a name="ubuntu-host"></a>

## Ubuntu 主机

使用 apt 安装发布版 DEB，将 `version` 设为安装包文件名中的版本号。每个安装包都会创建该版本专用的非特权服务账号和 systemd 单元。DEB 要求 `/opt` 归 `root` 所有，且仅允许所有者写入。

```bash
version=0.3.0
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

在管理页的“搜索设置”中选择 SearXNG，将端点设为 `http://127.0.0.1:8888/search`。在 `business.allowed_hosts` 中加入实际的外部主机名及所用端口，例如 `search.example.org` 和 `search.example.org:443`。使用 QCopilots WebUI 时，将准确的 `Origin` 值 `https://chat.vastarmorai.com` 加入 `business.origins`。保留回环绑定，以及本地客户端所需的准确主机和端口。源地址末尾省略斜杠。

为每个集成方或团队成员分配独立的客户端身份及授权码。QGIS 在 MCP Servers 页面填写该业务 Bearer。修改监听配置后，使用目标版本的服务账号检查配置，再重启该版本。

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
msiexec.exe /i "C:\Downloads\QCopilots-WebSearch-0.3.0-win64.msi" INSTALL_ROOT="C:\Program Files\QCopilots-WebSearch-Custom-0.3.0"
```

本机交互式用户可以读取 `admin.token`，用于登录设置页并管理搜索设置。运行配置、客户端令牌和搜索源密钥各自受文件 ACL 保护。在目标版本安装目录的 `bin` 下运行 `qcopilots-websearch.exe --SettingsUrl`，即可为正在运行的服务生成一次性链接。

请以管理员身份运行 MSI，并阅读安装程序显示的 Apache 许可条款和 Microsoft 运行库条款。安装包的签名状态以发布说明为准，未特别说明时为未签名。在 Windows 的“已安装的应用”中按版本选择要卸载的 MSI。卸载会移除该版本的程序文件和服务，并保留配置和数据。

将便携 ZIP 解压到私有可写目录，每个便携实例使用独立的解压目录。例如，将 `QCopilots-WebSearch-0.3.0-win64.zip` 下载到 `C:\Softwares\QCopilots-WebSearch`，解压后确认 `C:\Softwares\QCopilots-WebSearch\QCopilots-WebSearch-0.3.0-win64\bin\qcopilots-websearch.exe` 存在，避免重复嵌套同名目录。在解压目录运行 `.\bin\qcopilots-websearch.exe`，即可在前台启动服务。

首次启动会创建 `bin\data\runtime.json`，并生成客户端令牌和管理令牌。监听器准备就绪后，终端显示实际的业务、MCP、控制、设置地址、配置和数据路径，以及登录方法。默认业务端口为 3181，控制端口为 3182，设置端口为 3183。若首次初始化选用了其他端口，以启动信息和运行配置为准。服务运行期间，从另一个终端执行 `.\bin\qcopilots-websearch.exe --SettingsUrl`，打开该实例的设置页。

用 `--ConfigPath FILE` 指定其他配置文件。要初始化自定义路径，请使用 `--InitializeConfig --ConfigPath FILE`。可加上 `--SelectAvailablePorts` 为新配置选择可用的回环端口。新配置默认使用自动搜索，因此可以按需接入私有 SearXNG 实例。

<a name="windows-cloudflare-tunnel"></a>

### 在 Windows 上通过 Cloudflare Tunnel 发布服务

#### 同机 HTTP 回源快速设置

当 `cloudflared` 和 WebSearch 在同一台 Windows 主机上运行时，连接器可通过回环 HTTP 访问 WebSearch，无需配置源站证书。QGIS 和其他远程客户端通过 HTTPS 访问公网域名。以下以便携 ZIP 和 `search.example.org` 为例，请替换成自己的公网域名。

1. 在解压目录运行 `.\bin\qcopilots-websearch.exe` 完成首次启动。按 Ctrl+C 停止程序后，打开 `bin\data\runtime.json`，确认 `business.host` 为 `127.0.0.1` 并记下 `business.port`。下面假定端口是 3181。如果配置中是其他端口，后续步骤使用实际值。
2. 在 `business.allowed_hosts` 中保留现有的回环地址（如 `127.0.0.1:3181`），追加 `search.example.org` 和 `search.example.org:443`。将 `business.public_url` 设为 `https://search.example.org/mcp`，让设置页的连接步骤显示可复制的公网 MCP 地址。默认 HTTP 配置没有 `business.tls` 字段。从此前的 HTTPS 配置切换时，移除这一字段。保存文件，运行 `.\bin\qcopilots-websearch.exe --CheckConfig`，检查通过后再无参数启动程序。
3. 在 Cloudflare 的 **Networking → Tunnels → 目标隧道 → Routes** 编辑或添加 **Published application**。主机名填公网域名，Path 留空。在 **Service** 中选择 **HTTP**，**URL** 填 `127.0.0.1:3181`。若页面只有 **Service URL** 一栏，填写 `http://127.0.0.1:3181`。只发布业务端口，控制和管理端口供本机使用。[Cloudflare 路由设置](https://developers.cloudflare.com/tunnel/get-started/#publish-an-application)
4. 保持服务运行，在解压目录另开 PowerShell 进行公网验证。下列第一条命令应显示 `401`，最后一条应显示 `contract_version` 为 `1`、`service` 为 `ready`。示例使用首次启动生成的业务令牌。如果已通过页面或 CLI 轮换该令牌，请将 `$token` 设为此前保存的新 Bearer。轮换不会更新 `client.token`，注册表仅保存摘要，无法找回原码。

```powershell
curl.exe -sS -o NUL -w "%{http_code}\n" https://search.example.org/v1/status
$token = (Get-Content -Raw .\bin\data\client.token).Trim()
Invoke-RestMethod -Uri 'https://search.example.org/v1/status' -Headers @{ Authorization = "Bearer $token" } |
  Select-Object contract_version,service
```

在解压目录运行 `.\bin\qcopilots-websearch.exe --SettingsUrl --Open` 即可打开本机设置页。

`business.public_url` 是可选的展示配置，修改后重启服务生效。只接受完整 HTTPS 地址和精确的 `/mcp` 路径，不接受用户信息、查询或片段。它不会自动添加 Host 白名单、设置浏览器 Origin 或创建隧道路由。未设置时，设置页显示实际本地业务地址，不能据此声称公网已可达。使用 HTTPS 回源时，也应明确设置相同的公网 MCP 地址。

#### 可选的 HTTPS 回源

从 0.2.2 起，可按下列步骤在同一回环业务端口启用 HTTPS。示例使用 `https://127.0.0.1:3181`，公网域名仍为 `search.example.org`。

1. 以目标版本初始化配置。在新解压目录运行 `.\bin\qcopilots-websearch.exe --InitializeConfig --ConfigPath .\bin\data\runtime.json`。确认 3181 可由此实例独占，已有实例保留自己的版本目录和数据。
2. 准备用于 TLS 服务端身份验证的 PEM 证书链和配套的无口令 PEM 私钥。证书须包含 IP 类型的 SAN `127.0.0.1`。证书链将叶证书放在首位，随后附上中间证书。用 Windows ACL 将私钥读取权限限于运行 WebSearch 的账号和管理人员。为 `cloudflared` 服务账号提供私有根 CA **公钥证书**的可读路径。
3. 编辑生成的 `bin\data\runtime.json`，按下例修改其中的 `business`。相对证书路径以该配置文件所在目录为基准。示例把证书和私钥放在 `bin\data\tls`，也可以填写版本目录外的绝对路径。更新证书后重新检查配置并重启。

```json
{
  "business": {
    "host": "127.0.0.1",
    "port": 3181,
    "allowed_hosts": ["127.0.0.1:3181", "search.example.org", "search.example.org:443"],
    "origins": [],
    "tls": {
      "certificate_chain_file": "tls/origin-cert.pem",
      "private_key_file": "tls/origin-key.pem"
    }
  }
}
```

4. 从解压目录运行以下命令。`--CheckConfig` 成功后再启动服务。服务持续运行时，用另一个终端执行验证命令。

```powershell
.\bin\qcopilots-websearch.exe --ConfigPath .\bin\data\runtime.json --CheckConfig
if ($LASTEXITCODE -eq 0) { .\bin\qcopilots-websearch.exe }
```

5. 在 Cloudflare 的 **Networking → Tunnels** 选择隧道，打开 **Routes** 并编辑或新增 **Published application**。选择公网主机名并留空 Path。Service URL 填 `https://127.0.0.1:3181`。若界面分别显示 Type 和 URL，则选择 **HTTPS** 并输入 `127.0.0.1:3181`。业务监听器使用 HTTP/1.1，请保持 **HTTP/2 to origin** 关闭，保留默认的公网 Host 和 TLS 证书验证。对本例的 IP 证书，留空 **Origin Server Name**，保持 **Match SNI to Host** 关闭。使用私有 CA 时，在 **CA Pool** 填连接器本机的 CA 证书文件绝对路径。若证书使用 DNS 类型的 SAN，将 **Origin Server Name** 设为该 DNS 名称。控制和管理端口只供本机使用。[Cloudflare 路由设置](https://developers.cloudflare.com/tunnel/get-started/#publish-an-application) · [源站 TLS 参数](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/)
6. 逐台核对同一隧道的所有副本。每台连接器访问的是自己的 `127.0.0.1:3181`，都需能读取路由指定路径的 CA 文件，并连接到具有有效证书和相同业务凭据的服务。MCP 会话与已打开的网页快照属于处理请求的 WebSearch 实例，跨不同实例的后续调用可能找不到会话或页面。需要连续调用的验收应安排到同一实例。

以下示例在每台源站校验证书与接口，再从公网验证同一个接口。将第一条命令的 CA 路径替换为本机实际文件。本例私有 CA 未发布吊销信息，Windows 自带的 curl 使用 `--ssl-revoke-best-effort` 允许缺少该信息，同时验证 CA 和源站身份。未携带 Bearer 的源站检查应得到 HTTP 401。公网检查使用初始 `local` 客户端的 `client.token`，如果已更换令牌，请从实际凭据来源读取。成功时会显示 `contract_version` 为 1、`service` 为 `ready`。

```powershell
curl.exe --noproxy "*" --cacert .\bin\data\tls\origin-ca.pem --ssl-revoke-best-effort -sS -o NUL -w "%{http_code}\n" https://127.0.0.1:3181/v1/status
$token = (Get-Content -Raw .\bin\data\client.token).Trim()
Invoke-RestMethod -Uri 'https://search.example.org/v1/status' -Headers @{ Authorization = "Bearer $token" } |
  Select-Object contract_version,service
```

公网出现 502 时，先在报错副本上运行源站检查，核对监听端口、HTTP 或 HTTPS、证书有效期、SAN 和 CA 文件访问权限。401 表示需要核对该实例的业务令牌，403 时检查实际的 Host 和浏览器 Origin。仅访问公网根路径无法验收业务接口。设置页面使用独立的管理监听器。在源站另开终端运行 `.\bin\qcopilots-websearch.exe --SettingsUrl --Open` 可直接打开设置页。单独使用 `--SettingsUrl` 会打印链接，将输出粘贴到浏览器地址栏，两分钟内可登录一次。

##### 为测试源站签发证书

已有受信任证书的部署可直接使用已有证书链。若需为测试源站建立私有 CA，在一台管理机上选择专用的空目录，先通过 ACL 限制访问，再用 OpenSSL 3 执行以下 PowerShell 命令。前两条命令会分别生成签发用私钥和源站私钥。每台源站独立生成密钥及请求，可由同一 CA 签发。

```powershell
openssl req -x509 -newkey rsa:3072 -noenc -sha256 -days 1825 -subj "/CN=WebSearch Test Origin CA" `
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" `
  -keyout issuer-key.pem -out origin-ca.pem
openssl req -new -newkey rsa:3072 -noenc -sha256 -subj "/CN=127.0.0.1" `
  -addext "basicConstraints=critical,CA:FALSE" -addext "keyUsage=critical,digitalSignature,keyEncipherment" `
  -addext "extendedKeyUsage=serverAuth" -addext "subjectAltName=IP:127.0.0.1" -keyout origin-key.pem -out origin.csr
openssl x509 -req -in origin.csr -CA origin-ca.pem -CAkey issuer-key.pem -CAcreateserial `
  -days 365 -sha256 -copy_extensions copy -out origin-cert.pem
openssl verify -x509_strict -purpose sslserver -verify_ip 127.0.0.1 -CAfile origin-ca.pem origin-cert.pem
```

验证输出应为 `origin-cert.pem: OK`。源站监听器读取 `origin-cert.pem` 和 `origin-key.pem`。连接器的 **CA Pool** 使用 `origin-ca.pem`。将 `issuer-key.pem` 留在签发端，供证书续期使用。

<a name="manage-client-tokens"></a>

## 管理客户端令牌

登录管理页后，顶部可切换“访问授权”“搜索设置”和“服务状态”。进入“访问授权”，左侧列出客户端，右侧显示所选客户端的详情。添加客户端或替换已有客户端的授权码时，按以下三步操作。

1. **填写与生成**：填写客户端 ID，选择随机生成、从字符串生成或手动输入授权码。点击“下一步：确认保存”进入审核，此时新码尚未生效，替换操作的旧码仍然有效。
2. **保存并生效**：核对客户端和变更，点击“保存并生效”。成功后新码立即可用，替换操作的旧码随即不能用于新请求。
3. **复制连接资料**：复制已生效的 Bearer。其他 MCP 客户端还可复制服务地址，QCopilots 则保留已经登记的服务地址，在 MCP Servers 页面启用 Authorization 并粘贴 Bearer。停留在本步骤时可以重复复制，配置完成后点击“完成”。

客户端 ID 使用 1 至 64 个英文字母、数字、点、下划线或连字符。授权码接受 1 至 8192 个不含空格的可见 ASCII 字符，可以包含 `/`，例如 `xbyang/qcopilots-websearch`。该值是业务 Bearer，与管理页登录使用的 `admin.token` 分开。

“随机生成”使用密码学安全随机源生成 64 位小写十六进制授权码。“从字符串生成”将非空输入原文编码为 UTF-8，再计算 SHA-256 并输出 64 位小写十六进制字符。相同输入始终得到相同结果，前后空格、换行、大小写和 Unicode 字符均参与计算，不执行去空格或规范化。源字符串不发送到服务端，也不写入浏览器存储，生成后清空。生成只填写第一步的授权码，必须完成第二步保存才能生效。

第三步显示的授权码来自本次操作的页面内存，可以重复复制。点击完成、切换客户端或标签页、刷新页面、重新加载客户端列表或退出登录后，该临时值会清空。服务只保存摘要，之后无法从客户端详情找回原码。需要再次分发时，使用此前自行保存的原码，或替换为新授权码。

新增、更新和删除成功后立即影响后续请求，并在重启后保留。列表仅显示客户端 ID 和凭据来源，不返回授权码原文或摘要。更新已有客户端的授权码会保留其缓存身份，删除后重新创建同名客户端则使用新的身份，不能继承旧网页快照或 MCP 会话。删除最后一个业务客户端后，业务访问会被拒绝，管理员仍可登录设置页恢复授权。

首次持久化授权变更时，程序将运行配置中的客户端完整迁移到 `settings.directory` 下的 `client-registry.json`，默认位置为 `bin/data/state/client-registry.json`。此后该注册表是客户端凭据的唯一来源，修改 `runtime.json` 的 `clients` 不会覆盖它。已有的文件或环境变量凭据来源在迁移时保留，手动保存的授权码只存储 SHA-256 摘要。备份时保留整个私有数据目录。

CLI 也可生成或登记令牌。两条命令都会在标准错误输出中请求确认，需要输入 `y` 或 `yes`。设置令牌时，从标准输入读取 1 至 8192 个可见 ASCII 字符，并在交互式终端中隐藏输入，再在下一行确认。`--GenerateToken` 生成 64 个十六进制字符的随机业务令牌，并在保存后将新令牌写入标准输出。CLI 修改保留重启生效的方式。

```text
qcopilots-websearch --ConfigPath FILE --GenerateToken [--ClientId ID]
qcopilots-websearch --ConfigPath FILE --SetToken [--ClientId ID]
```

CLI 默认客户端 ID 为 `local`。对于新 ID，命令会添加条目。对于已有 ID，命令会替换凭据并保留其缓存身份。运行中的服务不会自动加载 CLI 写入，设置页会提示待重启的变更。重启后新授权生效，被替换的旧令牌失效。也可以在页面重新读取最新授权列表后执行保存，该次网页写入会一起应用此前待加载的 CLI 变更。

将令牌原文存入私有凭据存储，并通过安全渠道交给客户端。配置中的摘要不能用于连接服务或恢复令牌。单独运行 `--GenerateToken` 时，命令只生成并输出令牌。

对于 DEB 安装，以管理员身份生成凭据，再用该版本的服务账号检查配置并重启服务。

```bash
version=0.3.0
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
$version = '0.3.0'
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

在客户端的 MCP 服务器设置中输入 `https://search.example.org/mcp`，并将示例主机名替换为服务的实际主机名。启用 **Authorization**，在 **Bearer** 字段中粘贴授权码。确认客户端可列出四个工具，并能调用 `web_search_status`。[QGIS 接入](#qgis) 使用相同的公网 MCP 地址和授权码。

<a name="runtime-trust-and-proxies"></a>

## 运行时证书信任与代理

服务访问搜索源和网页时，出站 HTTPS 使用平台信任库。请在 HTTPS 端点配置完整的服务器证书链。Windows 可通过 AIA 获取中间证书，Linux 则使用端点提供的证书链和本机信任库。两种平台均省略在线 CRL 和 OCSP 查询。运行时请求直接连接目标地址，请配置可直接访问的 SearXNG 端点。构建配置中的代理仅用于下载依赖。[运行时架构](architecture.md)

## QGIS

从 QGIS 分支安装独立插件 `qcopilots_mcp_server_web_search`，其源码位于 `python/plugins/qcopilots_mcp_server_web_search`。Manager 继续管理本机插件生命周期，插件将 MCP 请求转发到已经部署的公网 WebSearch 服务。原生服务由部署者单独启动和维护。

<a name="give-qgis-its-own-credential"></a>

### 为 QGIS 分配独立凭据

在原生服务的“访问授权”标签页，为每位用户添加独立的客户端名称和授权码。完成“填写与生成”“保存并生效”后，在“复制连接资料”步骤复制 Bearer 并交给对应用户。用户在 QCopilots 的 MCP Servers 页面填写该码即可，无需在 QGIS 中生成或登记授权码。网页保存立即生效，无需重启原生服务。管理员登录令牌无需交给 QGIS 用户。

<a name="adapter-settings"></a>

### 在 MCP Servers 页面配置

1. 启动 **QCopilots MCP Server Web Search**，打开 **Configure Server**。
2. 内嵌页面保留 **Server URL** `https://qcopilots.localmachine/mcp/qcopilots.mcp_server_web_search`。该地址属于 QGIS 内部转发入口。
3. 启用 **Authorization**，在 **Bearer** 中填写服务管理员提供的业务授权码并保存。
4. 连接并确认能发现四个 WebSearch 工具，然后依次调用状态、搜索、网页打开和页内查找。

插件默认将这些请求转发到 `https://web-search.vastarmorai.com/mcp`。初始化、工具列表、工具调用、取消和会话关闭均使用远端 MCP 接口，客户端 Bearer 随请求传递。正常使用不需要 `QCOPILOTS_WEBSEARCH_BACKEND_TOKEN`、令牌文件、额外后端配置文件或搜索审批。客户端应保管授权码，插件不会用本机管理凭据替代它。

部署者使用其他公网域名时，可以通过已有的 `QCOPILOTS_WEBSEARCH_CONFIG` 或 `<QCOPILOTS_HOME>/web_search/config.json` 将 `backend_url` 指向实际的 HTTPS `/mcp` 地址。旧配置中没有路径的 HTTPS 源地址会在内存中补上 `/mcp`，无需修改旧文件。默认公网地址不需要这一额外配置。

<a name="start-and-troubleshoot"></a>

### 外部浏览器与故障排查

使用外部浏览器测试时，在启动 QGIS 前设置 `QCOPILOTS_EXTERNAL_BROWSER_ACCESS=ON`。页面使用本机插件的 MCP 地址 `http://127.0.0.1:48218/mcp` 和业务 Bearer，请求仍由插件转发到公网 HTTPS 地址。这一回环地址属于 QGIS 插件，与原生 WebSearch 的 3181 业务端口不同。

Manager 的本机健康检查使用独立的内部凭据，只表示插件 relay 是否运行。公网 WebSearch 离线时，本机健康检查仍可成功，但初始化、工具发现或调用会报告远端故障。原生服务的 `/health/live` 和 `/health/ready` 位于其独立控制监听器，不通过公网业务路由暴露。

当前转发支持 WebSearch 提供的 JSON Streamable HTTP MCP 交互，远端若返回不支持的 SSE 流，会明确失败。QGIS 不缓存远端工具列表来掩盖后端离线，也不会在工具调用前增加搜索确认。

| 现象 | 检查内容 |
| --- | --- |
| 401 或 `unauthorized` | 页面是否开启 Authorization，业务授权码是否存在、已轮换或被删除。CLI 修改需要重启后生效。 |
| 403 或 `forbidden` | 公网请求的 Host 是否被原生服务允许，浏览器直连时 Origin 是否匹配。 |
| `backend_unavailable` | 原生服务和 Cloudflared 是否运行，隧道是否指向该实例实际的业务端口。 |
| 会话或页面不存在 | 请求是否被多个独立源站分流，客户端身份是否被删除重建。 |
| `timeout` | 原生服务、搜索来源或公网连接是否超时。 |

测试完成后，停止本次启动的 QGIS、WebSearch 和其他测试进程。若本次启动了 Windows Cloudflared 服务，执行 `sc.exe stop Cloudflared`，确认服务停止后再结束测试。
