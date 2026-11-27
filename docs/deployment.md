<a name="team-deployment"></a>

# 团队部署

本部署示例适用于 10 至 50 人的团队，默认允许同时处理五项业务操作。搜索结果取决于上游服务的可用性。公开搜索引擎可能只返回部分结果、要求验证码或限制请求频率。服务遵守这些限制。

<a name="installed-versions-and-ports"></a>

## 安装版本与端口

MSI 和 DEB 安装包会为每个项目版本注册独立服务。首次安装时，每个版本都会创建自己的部署配置、客户端令牌、管理令牌、设置、搜索源密钥和缓存。运行配置和令牌位于该版本的 `bin/data` 目录，设置、搜索源密钥和缓存位于 `bin/data/state`。将同一版本重新安装到原目录时，会继续使用该版本的数据。

安装程序优先选择业务、控制和管理监听器的默认回环端口，依次为 3181、3182 和 3183。安装程序会跳过已被占用的端口，以及其他已安装版本配置中记录的端口，即使对应服务当前处于停止状态。选定的端口会写入该版本的运行配置，服务重启时继续使用这些端口。MCP 使用 `business.port`，健康检查和指标使用 `control.port`，设置页使用 `admin.port`。连接时请使用同一版本的令牌。

<a name="business-addresses-and-browser-access"></a>

## 业务地址与网页接入

业务接口供不同产品的网页、原生程序和服务端 API 客户端使用。任意合法 HTTP(S) 网页均可携带有效的客户端 Bearer 调用 `/mcp`，无需将每个产品的域名登记到服务配置。网页的 CORS 预检不要求 Bearer，实际业务请求始终要求有效 Bearer。业务接口不使用浏览器 Cookie 鉴权。原生程序和服务端客户端可以省略 `Origin`。

| 配置或请求信息 | 作用 |
| --- | --- |
| `business.allowed_hosts` | 服务接受的目标地址，对应 HTTP `Host` 请求头。例如客户端访问 `https://web-search.vastarmorai.com/mcp` 时通常发送 `Host: web-search.vastarmorai.com`。这里填写服务自己的地址，不填写调用方网页的地址。 |
| `Origin` 请求头 | 表示发起请求的网页来源，例如 `https://chat.vastarmorai.com`。业务服务检查它是否是合法的 HTTP(S) 来源，并返回所需的 CORS 响应头。 |
| `business.public_url` | 供设置页展示和复制的公网 MCP 地址。它不控制授权，不建立 Cloudflare 路由，也不证明公网已经连通。 |

新生成的配置默认包含以下业务设置。安装或初始化选择其他端口时，`port` 和回环 Host 使用实际端口。

```json
{
  "business": {
    "host": "127.0.0.1",
    "port": 3181,
    "allowed_hosts": ["127.0.0.1:3181", "web-search.vastarmorai.com"],
    "public_url": "https://web-search.vastarmorai.com/mcp"
  }
}
```

`business.host` 是本机监听地址，Cloudflare 或其他反向代理负责把公网流量送到它。使用自己的公网域名时，更新 `allowed_hosts` 和 `public_url`，同时配置对应的 DNS 与代理路由。Host 按请求中的实际值匹配，带显式端口的 Host 需要有对应条目。运行 `--CheckConfig` 后重启服务以应用变更。控制和管理监听器分别校验 Host 与 Origin，管理页写操作还要求有效会话和 CSRF 令牌。

## 通过 stdio 连接 llama-server

将以下内容保存为 `mcp.json`，将 `command` 换为当前平台可执行文件的绝对路径。启动 llama-server 时传入 `--mcp-servers-config mcp.json`。其 `GET /tools` 会列出 `websearch_web_search` 等四项工具。首次实际调用会重新启动子进程。

```json
{
  "mcpServers": {
    "websearch": {
      "command": "C:/Tools/QCopilots-WebSearch/bin/qcopilots-websearch.exe",
      "args": ["--Stdio"]
    }
  }
}
```

`--Stdio` 使用同版本可执行文件旁的 `data/runtime.json`。仅在确认该配置不存在时，才采用内置的自动搜索及进程私有的临时设置与缓存。共享部署配置的 stdio 进程使用后台管理的设置，页面快照始终归当前子进程所有。配置不兼容或不可读时，启动诊断写入 stderr，进程以非零状态退出。

需要自己的配置时，向 `args` 追加 `["--ConfigPath", "/absolute/path/to/runtime.json"]`。从安装或解压根目录的 `share/qcopilots-websearch/config/runtime.json` 将模板复制到用户自有目录，按需指定 `settings.directory`。DEB 的安装根目录为 `/opt/qcopilots-websearch-<已安装版本>`。安装服务的数据目录受限时，可以使用自己的配置或通过 HTTPS 与 Bearer 连接服务。

llama.cpp WebUI 中添加 Streamable HTTP 服务器，URL 填部署的 `/mcp` 地址，自定义请求头 `Authorization` 填 `Bearer <业务授权码>`。直接连接时关闭 WebUI 的 CORS 代理。发现工具后先调用 `web_search_status`，再用 `web_search` 验证选定的搜索后端。

<a name="ubuntu-host"></a>

## Ubuntu 主机

使用 apt 安装下载的 DEB。以下以 `v0.5.1` 为例。安装新版本时，将 `version` 和 `deb` 替换为实际版本号和文件名。新包文件名格式为 `QCopilots-WebSearch-<版本>-linux-amd64.deb`，systemd 单元名使用 `qcopilots-websearch-<版本>.service`。每个安装包都会创建该版本专用的非特权服务账号和 systemd 单元。DEB 要求 `/opt` 归 `root` 所有，且仅允许所有者写入。

```bash
version=0.5.1
package=qcopilots-websearch-$version
deb=qcopilots-websearch-0.5.1_amd64.deb
sudo apt install "./$deb"
sudo systemctl status "$package.service"
```

systemd 服务读取 `/opt/qcopilots-websearch-<version>/bin/data/runtime.json`。管理员可以编辑该文件，对应的服务账号可以读取。该账号拥有同目录下的 `client.token` 和 `admin.token`，两者的权限均为 0600。

运行目标版本的可执行文件，生成该版本的一次性管理链接。

```bash
sudo "/opt/$package/bin/qcopilots-websearch" --SettingsUrl
```

在服务所在主机的浏览器中打开链接。若要从远程浏览器访问，可通过 SSH 隧道连接到对应的回环地址，或配置专用 HTTPS 代理，并设置管理端的 `Host`、`Origin` 和公开源地址。

需要反向代理时，仅发布业务监听器上的 `/mcp`。以下 Caddy 配置将请求送到本机业务端口。将域名与端口替换为实际值，并将外部域名加入 `business.allowed_hosts`。

```caddyfile
search.example.org {
    handle /mcp {
        reverse_proxy 127.0.0.1:3181
    }
    handle {
        respond 404
    }
}
```

使用私有 SearXNG 时，在后台“搜索设置”中指定已有实例的搜索端点，例如 `http://127.0.0.1:8888/search`。私有后端、管理端口和控制端口仅供其预期客户端访问。

为每个集成方或团队成员分配独立的客户端身份及授权码。QGIS 在 MCP Servers 页面填写该业务 Bearer。修改监听配置后，使用目标版本的服务账号检查配置，再重启该版本。

```bash
account=$(systemctl show --property=User --value "$package.service")
sudo -u "$account" "/opt/$package/bin/qcopilots-websearch" --CheckConfig
sudo systemctl restart "$package.service"
```

代理应保留业务 Host、Origin、Authorization 和 MCP 会话头，并在客户端断开时取消上游请求。

## Windows

MSI 安装 `QCopilotsWebSearch-<version>` 系统服务，运行账号为虚拟账号 `NT SERVICE\QCopilotsWebSearch-<version>`。默认程序目录是 `%ProgramFiles%\QCopilots-WebSearch-<version>`。服务读取该目录中的 `bin\data\runtime.json`。

如需改用其他目录，请在以管理员身份打开的命令提示符中设置 `INSTALL_ROOT`。程序及其 `bin\data` 目录均位于指定位置。请为不同版本选择各自的安装目录。以下命令使用 `v0.5.1` 的 MSI。安装新版本时，使用下载的 `QCopilots-WebSearch-<版本>-win-amd64.msi`。

```cmd
msiexec.exe /i "C:\Downloads\QCopilots-WebSearch-0.5.1-win64.msi" INSTALL_ROOT="C:\Program Files\QCopilots-WebSearch-Custom-0.5.1"
```

本机交互式用户可以读取 `admin.token`，用于登录设置页并管理搜索设置。运行配置、客户端令牌和搜索源密钥各自受文件 ACL 保护。在目标版本安装目录的 `bin` 下运行 `qcopilots-websearch.exe --SettingsUrl`，即可为正在运行的服务生成一次性链接。

请以管理员身份运行 MSI，并阅读安装程序显示的 Apache 许可条款和 Microsoft 运行库条款。可在 PowerShell 中用 `Get-AuthenticodeSignature -LiteralPath <MSI 文件路径>` 查看所下载 MSI 的签名状态。在 Windows 的“已安装的应用”中按版本选择要卸载的 MSI。卸载会移除该版本的程序文件和服务，并保留配置和数据。

将便携 ZIP 解压到私有可写目录，每个便携实例使用独立的解压目录。例如，将 `QCopilots-WebSearch-0.5.1-win64.zip` 下载到 `C:\Softwares\QCopilots-WebSearch`，解压后确认 `C:\Softwares\QCopilots-WebSearch\QCopilots-WebSearch-0.5.1-win64\bin\qcopilots-websearch.exe` 存在，避免重复嵌套同名目录。新版本的 `QCopilots-WebSearch-<版本>-win-amd64.zip` 内含与文件主体同名的顶层目录。在含有 `bin` 的目录运行 `.\bin\qcopilots-websearch.exe`，即可在前台启动服务。

首次启动会创建 `bin\data\runtime.json`，并生成客户端令牌和管理令牌。监听器准备就绪后，终端显示实际的业务、MCP、控制、设置地址、配置和数据路径，以及登录方法。无参数启动全新的便携实例时，业务、控制和设置端口固定为 3181、3182、3183，任一端口已占用时启动会失败。已有配置使用其中记录的端口。服务运行期间，从另一个终端执行 `.\bin\qcopilots-websearch.exe --SettingsUrl`，打开该实例的设置页。

用 `--ConfigPath FILE` 指定其他配置文件。要初始化自定义路径，请使用 `--InitializeConfig --ConfigPath FILE`。可加上 `--SelectAvailablePorts` 为新配置选择可用的回环端口。新配置默认使用自动搜索，因此可以按需接入私有 SearXNG 实例。

<a name="windows-cloudflare-tunnel"></a>

### 在 Windows 上通过 Cloudflare Tunnel 发布服务

#### 同机 HTTP 回源快速设置

当 `cloudflared` 和 WebSearch 在同一台 Windows 主机上运行时，连接器可通过回环 HTTP 访问 WebSearch，无需配置源站证书。QGIS 和其他远程客户端通过 HTTPS 访问公网域名。以下以便携 ZIP 和 `search.example.org` 为例，请替换成自己的公网域名。

1. 在解压目录运行 `.\bin\qcopilots-websearch.exe` 完成首次启动。按 Ctrl+C 停止程序后，打开 `bin\data\runtime.json`，确认 `business.host` 为 `127.0.0.1` 并记下 `business.port`。下面假定端口是 3181。如果配置中是其他端口，后续步骤使用实际值。
2. 在 `business.allowed_hosts` 中保留现有的回环地址（如 `127.0.0.1:3181`），追加 `search.example.org` 和 `search.example.org:443`。将 `business.public_url` 设为 `https://search.example.org/mcp`，让设置页的连接步骤显示可复制的公网 MCP 地址。使用 HTTP 回源时，配置中应省略 `business.tls`。保存文件，运行 `.\bin\qcopilots-websearch.exe --CheckConfig`，检查通过后再无参数启动程序。
3. 在 Cloudflare 的 **Networking → Tunnels → 目标隧道 → Routes** 编辑或添加 **Published application**。主机名填公网域名，Path 留空。在 **Service** 中选择 **HTTP**，**URL** 填 `127.0.0.1:3181`。若页面只有 **Service URL** 一栏，填写 `http://127.0.0.1:3181`。只发布业务端口，控制和管理端口供本机使用。[Cloudflare 路由设置](https://developers.cloudflare.com/tunnel/get-started/#publish-an-application)
4. 保持服务运行，在解压目录另开 PowerShell 进行公网验证。下列第一条命令应显示 `401`，随后的初始化请求应显示协商的协议版本。示例使用首次启动生成的业务令牌。如果已通过页面或 CLI 轮换该令牌，请将 `$token` 设为此前保存的新 Bearer。轮换不会更新 `client.token`，新保存的授权码可在本机管理页选择该客户端后按需查看或复制。

```powershell
curl.exe -sS -o NUL -w "%{http_code}\n" https://search.example.org/mcp
$token = (Get-Content -Raw .\bin\data\client.token).Trim()
$headers = @{ Authorization = "Bearer $token"; Accept = 'application/json, text/event-stream' }
$body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"deployment-check","version":"1"}}}'
$reply = Invoke-WebRequest -UseBasicParsing -Method Post -Uri 'https://search.example.org/mcp' -ContentType 'application/json' -Headers $headers -Body $body
($reply.Content | ConvertFrom-Json).result.protocolVersion
$headers['Mcp-Session-Id'] = [string]($reply.Headers['Mcp-Session-Id'] | Select-Object -First 1)
$headers['MCP-Protocol-Version'] = '2025-11-25'
Invoke-WebRequest -UseBasicParsing -Method Delete -Uri 'https://search.example.org/mcp' -Headers $headers | Out-Null
```

在解压目录运行 `.\bin\qcopilots-websearch.exe --SettingsUrl --Open` 即可打开本机设置页。

`business.public_url` 是展示配置，新生成文件默认写入 `https://web-search.vastarmorai.com/mcp`，修改后重启服务生效。只接受完整 HTTPS 地址和精确的 `/mcp` 路径，不接受用户信息、查询或片段。它不会自动添加 Host 白名单、放宽授权或创建隧道路由。省略该字段时，设置页显示实际本地业务地址。显示任何地址均不能证明公网已可达，仍须执行上面的连接检查。使用 HTTPS 回源时，也应设置实际的公网 MCP 地址。

#### 可选的 HTTPS 回源

可在同一回环业务端口启用 HTTPS。示例使用 `https://127.0.0.1:3181` 和公网域名 `search.example.org`。

1. 以目标版本初始化配置。在新解压目录运行 `.\bin\qcopilots-websearch.exe --InitializeConfig --ConfigPath .\bin\data\runtime.json`。确认 3181 可由此实例独占，已有实例保留自己的版本目录和数据。
2. 准备用于 TLS 服务端身份验证的 PEM 证书链和配套的无口令 PEM 私钥。证书须包含 IP 类型的 SAN `127.0.0.1`。证书链将叶证书放在首位，随后附上中间证书。用 Windows ACL 将私钥读取权限限于运行 WebSearch 的账号和管理人员。为 `cloudflared` 服务账号提供私有根 CA **公钥证书**的可读路径。
3. 编辑生成的 `bin\data\runtime.json`，按下例修改其中的 `business`。相对证书路径以该配置文件所在目录为基准。示例把证书和私钥放在 `bin\data\tls`，也可以填写版本目录外的绝对路径。更新证书后重新检查配置并重启。

```json
{
  "business": {
    "host": "127.0.0.1",
    "port": 3181,
    "allowed_hosts": ["127.0.0.1:3181", "search.example.org", "search.example.org:443"],
    "public_url": "https://search.example.org/mcp",
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

以下示例在每台源站校验证书与接口，再从公网验证同一个接口。将第一条命令的 CA 路径替换为本机实际文件。本例私有 CA 未发布吊销信息，Windows 自带的 curl 使用 `--ssl-revoke-best-effort` 允许缺少该信息，同时验证 CA 和源站身份。未携带 Bearer 的源站检查应得到 HTTP 401。公网检查使用初始 `local` 客户端的 `client.token`，如果已更换令牌，请从实际凭据来源读取。成功时会显示协商的协议版本。

```powershell
curl.exe --noproxy "*" --cacert .\bin\data\tls\origin-ca.pem --ssl-revoke-best-effort -sS -o NUL -w "%{http_code}\n" https://127.0.0.1:3181/mcp
$token = (Get-Content -Raw .\bin\data\client.token).Trim()
$headers = @{ Authorization = "Bearer $token"; Accept = 'application/json, text/event-stream' }
$body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"deployment-check","version":"1"}}}'
$reply = Invoke-WebRequest -UseBasicParsing -Method Post -Uri 'https://search.example.org/mcp' -ContentType 'application/json' -Headers $headers -Body $body
($reply.Content | ConvertFrom-Json).result.protocolVersion
$headers['Mcp-Session-Id'] = [string]($reply.Headers['Mcp-Session-Id'] | Select-Object -First 1)
$headers['MCP-Protocol-Version'] = '2025-11-25'
Invoke-WebRequest -UseBasicParsing -Method Delete -Uri 'https://search.example.org/mcp' -Headers $headers | Out-Null
```

公网出现 502 时，先在报错副本上运行源站检查，核对监听端口、HTTP 或 HTTPS、证书有效期、SAN 和 CA 文件访问权限。401 表示需要核对该实例的业务令牌，403 时检查实际 Host 是否在允许列表中，以及浏览器 Origin 是否为合法的单一 HTTP(S) 来源。仅访问公网根路径无法验收业务接口。设置页面使用独立的管理监听器。在源站另开终端运行 `.\bin\qcopilots-websearch.exe --SettingsUrl --Open` 可直接打开设置页。单独使用 `--SettingsUrl` 会打印链接，将输出粘贴到浏览器地址栏，两分钟内可登录一次。

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

客户端 ID 使用 1 至 64 个英文字母、数字、点、下划线或连字符。授权码接受 1 至 8192 个不含空格的可见 ASCII 字符，可以包含 `/`。该值是业务 Bearer，与管理页登录使用的 `admin.token` 分开。

“随机生成”使用密码学安全随机源生成 64 位小写十六进制授权码。“从字符串生成”将非空输入原文编码为 UTF-8，再计算 SHA-256 并输出 64 位小写十六进制字符。相同输入始终得到相同结果，前后空格、换行、大小写和 Unicode 字符均参与计算，不执行去空格或规范化。源字符串不发送到服务端，也不写入浏览器存储，生成后清空。生成只填写第一步的授权码，必须完成第二步保存才能生效。

第三步显示本次保存的授权码，可以重复复制。已保存客户端的详情也提供独立的显示、隐藏和复制操作，默认隐藏，点击时才读取单条原文。切换客户端或标签页、刷新、注销、删除或替换时清空页面原文，迟到的响应不会恢复它。无需为了再次复制而替换授权码。

新增、更新和删除成功后立即影响后续请求，并在重启后保留。列表仅显示客户端 ID 和凭据来源，不返回授权码原文或摘要。更新已有客户端的授权码会保留其缓存身份，删除后重新创建同名客户端则使用新的身份，不能继承旧网页快照或 MCP 会话。删除最后一个业务客户端后，业务访问会被拒绝，管理员仍可登录设置页恢复授权。

`runtime.json` 的 `clients` 提供初始授权列表。第一次持久化授权变更时，程序用完整列表和本次变更创建 `settings.directory/client-registry.json`，默认位于 `bin/data/state/client-registry.json`。此后以注册表为准，文件和环境变量来源均可继续使用。管理页和 CLI 保存手动授权时，将原文和 SHA-256 摘要原子写入受文件权限保护的注册表，供管理员按需查看，认证使用摘要。注册表容量上限为 2 MiB。备份时保留整个私有数据目录并保护其访问权限，不将原文注册表提交到版本控制或诊断日志。

CLI 也可生成或登记令牌。两条命令都会在标准错误输出中请求确认，需要输入 `y` 或 `yes`。设置令牌时，从标准输入读取 1 至 8192 个可见 ASCII 字符，并在交互式终端中隐藏输入，再在下一行确认。`--GenerateToken` 生成 64 个十六进制字符的随机业务令牌，并在保存后将新令牌写入标准输出。CLI 修改保留重启生效的方式。

```text
qcopilots-websearch --ConfigPath FILE --GenerateToken [--ClientId ID]
qcopilots-websearch --ConfigPath FILE --SetToken [--ClientId ID]
```

CLI 默认客户端 ID 为 `local`。对于新 ID，命令会添加条目。对于已有 ID，命令会替换凭据并保留其缓存身份。运行中的服务不会自动加载 CLI 写入，设置页会提示待重启的变更。重启后新授权生效，被替换的旧令牌失效。也可以在页面重新读取最新授权列表后执行保存，该次网页写入会一起应用此前待加载的 CLI 变更。

通过安全渠道将令牌交给客户端。通过管理页或 CLI 保存的授权码可在本机管理页再次显示或复制。仅配置 SHA-256 摘要的凭据可用于认证，替换授权码后即可查看新码。文件或环境变量来源读取失败会明确提示，外部更新尚未进入运行快照时会标记未生效。单独运行 `--GenerateToken` 时，命令只生成并输出令牌。

对于 DEB 安装，以管理员身份生成凭据，再用该版本的服务账号检查配置并重启服务。

```bash
read -r -p '输入要管理的已安装版本号: ' version
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
$version = Read-Host '输入要管理的已安装版本号'
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

使用目标版本业务监听器对应的 HTTPS 端点。在 `business.allowed_hosts` 中加入该端点准确的主机和端口。客户端从任意合法 HTTP(S) 网页直接连接时，无需登记页面源地址。HTTPS 端点负责将客户端的 Bearer 凭据转发给 QCopilots-WebSearch，服务对实际业务请求进行鉴权。

在客户端的 MCP 服务器设置中输入 `https://search.example.org/mcp`，并将示例主机名替换为服务的实际主机名。启用 **Authorization**，在 **Bearer** 字段中粘贴授权码。确认客户端可列出四个工具，并能调用 `web_search_status`。[QGIS 接入](#qgis) 使用相同的公网 MCP 地址和授权码。

<a name="runtime-trust-and-proxies"></a>

## 运行时证书信任与代理

服务访问搜索源和网页时，出站 HTTPS 默认使用平台信任库。请在 HTTPS 端点配置完整的服务器证书链。Windows 可通过 AIA 获取中间证书，Linux 则使用端点提供的证书链和本机信任库。两种平台均省略在线 CRL 和 OCSP 查询。运行时请求直接连接目标地址，构建配置中的代理仅用于下载依赖。[运行时架构](architecture.md)

部署者可在 `runtime.json` 顶层的 `provider` 对象中指定私有 SearXNG 的 `url`、`ca_file` 及 `token_file` 或 `token_env`。只有“搜索设置”中的 SearXNG 端点与 `provider.url` 完全匹配时，服务才使用该 CA 和部署凭据。若还在管理页为 SearXNG 保存了密钥，则优先使用管理页的密钥。

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

插件默认将这些请求转发到 `https://web-search.vastarmorai.com/mcp`。初始化、工具列表、工具调用、取消和会话关闭均使用远端 MCP 接口，客户端 Bearer 随请求传递。客户端应保管自己的业务授权码。

也可以在页面中新增自定义 Server URL，直接填写公网 `https://web-search.vastarmorai.com/mcp` 并启用业务 Bearer。此时浏览器直接发出请求，由公网服务处理 CORS。使用 Manager 虚拟地址时，请求经 QGIS 原生桥和本机插件转发，插件不把页面的 Origin 发送到公网。两种路径均要求公网 Host 被允许且 Bearer 有效，`business.public_url` 只影响设置页展示，不决定实际请求发往哪里。

部署者使用其他公网域名时，可通过 `QCOPILOTS_WEBSEARCH_CONFIG` 指定配置文件，或使用 `<QCOPILOTS_HOME>/web_search/config.json`。将 `backend_url` 设为实际的 HTTPS `/mcp` 地址。只填写 HTTPS 源地址时，插件会在请求时补上 `/mcp`。

<a name="start-and-troubleshoot"></a>

### 外部浏览器与故障排查

使用外部浏览器测试时，在启动 QGIS 前设置 `QCOPILOTS_EXTERNAL_BROWSER_ACCESS=ON`。页面使用本机插件的 MCP 地址 `http://127.0.0.1:48218/mcp` 和业务 Bearer，请求仍由插件转发到公网 HTTPS 地址。这一回环地址属于 QGIS 插件，与原生 WebSearch 的 3181 业务端口不同。本机插件仍只接受 Manager 允许的网页来源，公网业务接口的开放 CORS 不改变该本机限制。

Manager 的本机健康检查使用独立的内部凭据，只表示插件 relay 是否运行。公网 WebSearch 离线时，本机健康检查仍可成功，但初始化、工具发现或调用会报告远端故障。原生服务的 `/health/live` 和 `/health/ready` 位于其独立控制监听器，不通过公网业务路由暴露。

当前转发支持 WebSearch 提供的 JSON Streamable HTTP MCP 交互，远端若返回不支持的 SSE 流，会明确失败。QGIS 不缓存远端工具列表来掩盖后端离线，也不会在工具调用前增加搜索确认。

| 现象 | 检查内容 |
| --- | --- |
| 401 或 `unauthorized` | 页面是否开启 Authorization，业务授权码是否存在、已轮换或被删除。CLI 修改需要重启后生效。 |
| 403 或 `forbidden` | 公网 Host 是否在允许列表中，公网直连的 Origin 是否为合法 HTTP(S) 来源。连接本机插件时，页面来源仍须被 Manager 允许。 |
| `backend_unavailable` | 原生服务和 Cloudflared 是否运行，隧道是否指向该实例实际的业务端口。 |
| 会话或页面不存在 | 请求是否被多个独立源站分流，客户端身份是否被删除重建。 |
| `timeout` | 原生服务、搜索来源或公网连接是否超时。 |

测试完成后，停止本次启动的 QGIS、WebSearch 和其他测试进程。若本次启动了 Windows Cloudflared 服务，执行 `sc.exe stop Cloudflared`，确认服务停止后再结束测试。
