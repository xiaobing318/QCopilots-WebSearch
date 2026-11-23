<a name="version-1-interfaces"></a>

# 版本 1 接口

调用 HTTP `/mcp` 时，客户端通过 `Authorization: Bearer <client-token>` 提供业务凭据。浏览器预检除外。
`Bearer` 鉴权方案名不区分大小写。每个客户端都有独立身份。请求的 Host 必须与 `business.allowed_hosts` 中的条目匹配。
任意合法 HTTP(S) 网页均可携带业务 Bearer 接入，不再提供 `business.origins` 配置。带 Origin 的请求必须提供单一合法的 HTTP(S) 来源，不能是 `null`、多值或重复头，也不能包含用户信息、路径、查询或片段。原生和服务端客户端可以省略 Origin。远程客户端使用 HTTPS 并验证服务端证书。

浏览器 CORS 预检不要求 Bearer，预检成功不代表取得业务授权。服务为合法网页来源返回对应的 `Access-Control-Allow-Origin` 和 `Vary: Origin`，允许业务接口所需的请求方法和头，并暴露 MCP 会话等响应头。实际业务请求仍验证 Bearer，业务接口不使用 Cookie 凭据，也不返回 `Access-Control-Allow-Credentials`。控制和管理监听器保留各自的 Origin 及鉴权限制。

| 接口 | 请求 |
| --- | --- |
| MCP HTTP | 向 `/mcp` 发送 JSON-RPC POST 请求，协议版本为 `2025-11-25` 或 `2025-06-18` |
| MCP stdio | `--Stdio [--ConfigPath FILE]`，每行一条 JSON-RPC 消息，支持两个 HTTP 版本及 `2024-11-05` |

输入 JSON Schema 位于 `contracts/v1`。`contracts/v1/tools.json` 包含各工具的标题、描述和注解，以及 MCP
`instructions` 文本。MCP `tools/list` 按会话版本发布工具定义。2024 版提供名称、描述和输入 Schema，调用结果以可解析的 JSON 文本及 `isError` 返回。两个 2025 版本还提供 `structuredContent`。为兼容会拒绝顶层
`oneOf`、`anyOf`、`allOf` 或 `not` 的模型 API，输入 Schema 使用普通的 Draft 7 对象。
QGIS 插件通过 MCP 转发初始化、工具发现和工具调用，工具定义及输入校验由远端服务负责。

<a name="search"></a>

## 搜索

```json
{"query":"QGIS spatial index", "limit":10, "language":"en", "domains":["qgis.org"], "time_range":"any"}
```

`limit` 默认为 10，最大为 20。搜索和缓存前会合并查询中的连续空白。
SearXNG 内部最多读取三页上游结果，其他来源只取一页。该接口不提供分页游标。达到结果上限、遇到空页，
或当前页没有出现此前未见过的 URL 时，服务会停止读取。严格的域名过滤可能使返回数少于请求数。
服务在上游搜索完成后统一规范化主机名，并检查域名及其子域名是否匹配。
例如 `notexample.org` 不匹配 `example.org`。

跨页去重时，忽略 URL 的协议、开头的 `www.`、末尾的斜杠、片段、默认端口，以及
`utm_*`、`gclid` 和 `fbclid` 参数。多个结果命中同一个去重键时，保留其中第一个符合域名条件的 URL。

`time_range` 接受 `any`、`day`、`week`、`month` 和 `year`。支持时间范围的来源将其作为提示，结果的发布日期可能超出该范围。自动模式回退到 Bing 时会在响应的 warnings 中告知此次搜索未应用时间提示。每条结果包含 `title`、`url`、`snippet`、`sources` 和
`published_at`。`published_at` 使用 UTC ISO 8601 格式，例如 `2024-05-01T10:30:00Z`。
上游未提供可用日期时，该字段为 `null`。响应中的 `provider` 标识实际提供结果的来源。
响应还可带有上游提供的 `corrections` 和 `suggestions`，两者分别最多 5 条，每条最多 200 个字符。

搜索结果的状态为 `ok` 或 `partial`。所有上游引擎均失败时会返回错误。搜索成功但无结果时会正常返回空列表。
每个失败的引擎在 `failures` 中只出现一次，条目格式为 `{"engine", "reason": "unresponsive", "detail"}`，
其中 `detail` 是上游给出的原因，最多 200 个字符。如果前几页已经取得结果，后续页面请求失败或超时，
服务会以 `partial` 状态返回已有结果，并附上 `{"engine": "provider", "reason": <error code>}`。
自动搜索依次尝试 DuckDuckGo HTML、DuckDuckGo Lite 和 Bing。遇到超时或 5xx 响应时可重试一次。
服务分别对各来源控制请求间隔和冷却时间。响应包含警告、获取时间和缓存状态。

在设置中选择 Perplexity 或 DeepSeek，即可使用相应的结构化搜索 API。DeepSeek 接受规范化后最多 128 个
Unicode 字符的查询，超长时服务返回 `invalid_argument`。域名过滤仍由服务在本地执行。
[Perplexity Search API](https://docs.perplexity.ai/api-reference/search-post)
· [DeepSeek Search API](https://api-docs.deepseek.com/api/search/)

对于 `ok` 结果，`cache.ttl_seconds` 取配置的搜索缓存有效期。包含结果的 `partial` 响应最多缓存
30 秒，无结果的 `partial` 响应不写入缓存，该字段为 0。写入缓存失败时，搜索仍返回此次结果。
当请求复用刚刚完成的相同搜索的结果时，`cache.hit` 也为 `true`。

<a name="reading-and-finding"></a>

## 网页读取与文本查找

`web_open` 要求在 `url` 和 `page_id` 中恰好提供一项，两项均提供或均省略时返回
`invalid_argument`。该工具只获取公开的 HTTP(S) 资源。返回内容是限制了大小的快照，包含带行号的正文、
链接、来源信息、截断标记，以及当前客户端专用的页面 ID。默认从第 1 行开始返回 200 行，最多返回 500 行。
使用 `next_start_line` 和相同的页面 ID 继续读取同一份快照，可以避免重新请求可能已经变化的页面。
读完最后一行后，`next_start_line` 为 `null`。

每次响应只列出链接文字起始行位于当前返回范围内的链接。后续读取不会重复返回前面的链接。
链接条目格式为 `{"url", "text", "line"}`。`text` 是合并连续空白后的链接文字，最多 200 个字符，也可以为空。
`line` 是文字开始的行号，从 1 起算。服务会移除链接片段。URL 重复时，优先保留首次出现的条目。
如果首条没有链接文字，则改用后续含有文字的条目。指向当前页面自身或非 HTTP(S) 目标的链接会被跳过。
每页最多保留 200 个链接，`links_truncated` 标记是否达到上限。

`web_find` 在缓存的快照中查找指定的字面文本，默认采用不区分大小写的 Unicode 匹配。
查找词按普通文本处理。默认最多返回 20 行匹配结果，指定上限时可以返回最多 100 行。
页面 ID 未知、已过期或属于其他客户端时，工具返回 `page_unavailable`。重新打开原 URL 可获取新快照。

该工具只提取网页文本，不执行 JavaScript、不遵循网页内嵌指令、不提取 PDF，也不调用大语言模型生成答案。
将返回的文本视为未经信任的资料。

<a name="content-and-text-format"></a>

### 内容类型与文本格式

服务将 `text/html` 和 `application/xhtml+xml` 按 HTML 读取。其他 `text/*` 类型、`application/json`、
`application/*+json`、`application/xml` 和 `application/*+xml` 按纯文本读取。缺少 Content-Type 时，
服务先检查正文是否含有 HTML 标记，再检查是否包含二进制字节。其他不受支持的内容返回 `unsupported_content`。

HTML 正文以简化的 Markdown 输出，便于代理读取页面结构。

- 标题以 `#` 到 `######` 开头。
- 无序列表项以 `- ` 开头，有序列表项以 `N. ` 开头，并遵循 `start` 指定的起始序号。
  嵌套列表每层缩进两个空格，最多八层。
- 表格每行占一行文本，单元格之间用 ` | ` 分隔。
- `pre`、`textarea`、`listing`、`xmp` 和纯文本资源保留缩进与制表符。其他位置会合并连续空白。
- 只有后面有内容时才输出格式标记。链接作为独立字段提供。

可读内容的根元素依次取 `<main>`、页面上唯一的 `<article>` 或 `<body>`。
`header` 和 `footer` 仅在 `article`、`section`、`main` 和 `aside` 外部时被移除。
ARIA 的 `banner` 和 `contentinfo` 角色适用同一规则，`role="navigation"` 与 `nav` 一样会被跳过。
其他被跳过的元素包括 `script`、`style`、`noscript`、`template`、`svg`、`iframe`、
`object`、`embed`、`canvas`、`video`、`audio`，以及带有 `hidden`（`until-found` 除外）、
`aria-hidden="true"`、内联 `display:none` 或 `visibility:hidden` 的元素。
标题依次取 `<title>`、`og:title`、第一个 `h1` 或 URL。

服务从正文和标题中移除可能使文本不可见的控制字符与格式字符，包括 U+00AD、U+200B、U+2060、
U+202A 至 U+202E、U+2066 至 U+2069 以及 U+E0000 至 U+E007F。
服务保留 U+FEFF、U+200C 和 U+200D，供实际书写系统和表情符号使用。

解码时按以下顺序选用编码。先检查 BOM，再检查受支持的 HTTP charset，然后检查文件前 4 KiB 中受支持的
HTML meta 声明，最后使用 UTF-8。服务会忽略未知的编码标签。Lexbor 支持的编码包括 UTF-8、UTF-16、
Windows-1252、GBK、GB18030 和 Big5。非法字节序列会替换为 U+FFFD。
如果替换字符占解码文本的比例超过 1%，或输入带有 UTF-32 BOM，则返回 `unsupported_content`。

| 资源 | 限制 |
| --- | --- |
| 压缩传输内容和解码后的正文 | 各 5 MiB |
| 文本提取读取的编码字节 | 1 MiB |
| HTML 解析 | 最多插入 100,000 个节点，最多同时打开 512 个元素，最多保留 1,000 个活动格式元素 |
| HTML 遍历 | 最多 100,000 个节点，最大深度 512 |
| 提取的文本 | 200,000 个 Unicode 字符，10,000 行 |
| 单行长度 | 1,000 个 Unicode 字符 |
| 提取的链接 | 200 条 |
| 服务负载 | 240 KiB |
| 完整 MCP 响应 | 1 MiB |

只有超出传输限制时才返回 `response_too_large`。输入超出其他读取、解析、遍历或文本限制时，
服务保留可读部分并设置 `truncated`。为满足负载大小限制，网页读取和文本查找可能减少返回的行和链接。
此时可用 `next_start_line` 接着读取省略的正文行或匹配行。

<a name="results-and-errors"></a>

## 结果与错误

工具成功返回的负载包含 `contract_version: 1`。增加字段时保留该版本号。
stdio 的 `2024-11-05` 客户端读取 JSON 文本块和 `isError`。两个 2025 版本将相同的负载分别放在 `structuredContent` 和一个 JSON 文本内容块中。
业务失败或参数违反输入 Schema 时，响应使用 `isError: true`，并附上本节所示的错误对象，便于模型修正调用。
错误消息指出第一个未通过校验的字段，例如
`Input does not match the schema at /limit: instance exceeds maximum of 20.`。
只有调用未知工具名称时，才返回 JSON-RPC 参数无效错误。此版本不声明 `outputSchema`。

JSON 输入上限为 1 MiB，容器最大嵌套深度为 64。服务在构建 JSON 树前拒绝嵌套过深的输入。
序列化请求 ID 的上限为 64 KiB，包含 JSON 引号和转义。超大的 ID 会收到带 `id: null` 的 Invalid Request，
不创建会话，也不执行工具。客户端必须使用符合限制的 ID，才能将响应和请求对应起来。
对于已接受的请求 ID，服务会在响应中保留该 ID。如果完整响应超出 1 MiB，则返回 JSON-RPC 错误。
Stdio 响应也受 1 MiB 上限约束。队列最多容纳 32 条消息，占用不超过 4 MiB，正在写入的消息也计入。
关闭 stdin 后，服务会等待短暂的输出排空期，再取消阻塞的 stdout 写入。即使客户端停止读取，进程也能退出。

HTTP 鉴权和请求头错误使用相应的 HTTP 状态码。工具执行失败时，错误对象放在 MCP 工具结果内。例如

```json
{"error":{"code":"busy","message":"Service capacity is exhausted.","retryable":true}}
```

错误响应不包含成功结果的 `contract_version` 字段。可选的 `reason` 给出稳定的具体原因，`parameters` 仅包含经过限制的非秘密客户端标识 `client_id`。客户端应先依据 HTTP 状态码或 MCP 错误类型判断请求是否成功，
再解析成功响应的内容。`retryable` 指示稍后重试同一调用是否可能成功。HTTP 429 响应还带有 `Retry-After`
响应头。上游限流时，错误对象可以包含与该头对应的 `retry_after_seconds` 字段。

| 错误码 | 对应错误状态（工具调用见 `isError`） | 可重试 | 含义 |
| --- | --- | --- | --- |
| `invalid_argument` | 400 | 否 | 输入违反对应版本的 Schema，或未在 `url` 与 `page_id` 之间恰好选择一项 |
| `unauthorized` | 401 | 否 | 缺少客户端的独立凭据，或提供的凭据无效 |
| `forbidden` | 403 | 否 | 业务 Host 不在允许列表中，或请求带有不合法的 Origin |
| `tool_disabled` | 403 | 否 | 搜索或网页读取开关已关闭 |
| `destination_blocked` | 403 | 否 | 公开资源的获取策略拒绝了目标，包括从 HTTPS 重定向到 HTTP |
| `page_unavailable` | 404 | 否 | 当前客户端身份无法访问该快照 |
| `request_too_large` | 413 | 否 | 请求超出大小限制 |
| `unsupported_content` | 415 | 否 | 不支持该媒体类型或编码，或文本解码异常超过容许范围 |
| `busy` | 429 | 是 | 业务处理或上游调用已达到容量上限 |
| `rate_limited` | 429 | 是 | 上游限制了请求速率，或所有自动搜索来源都处于冷却期 |
| `cancelled` | 499 | 否 | 请求已取消 |
| `internal_error` | 500 | 否 | 服务内部出现意外错误 |
| `response_too_large` | 502 | 否 | 上游传输内容超出限制 |
| `provider_unavailable` | 502 | 暂时性故障可重试 | 搜索来源未能提供结果 |
| `upstream_http_error` | 502 | 上游返回 429、5xx 或连接失败时可重试 | 网页读取失败。消息包含 HTTP 状态码，或无法解析主机名等可公开的原因 |
| `provider_not_configured` | 503 | 否 | 所选来源需要 API 密钥 |
| `backend_unavailable` | 503 | 是 | 服务正在停止 |
| `cache_unavailable` | 503 | 依据具体原因 | 无法安全使用私有缓存，详见 `reason` |
| `timeout` | 504 | 是 | 操作超出时限 |

MCP HTTP 先进行初始化，随后在请求头中携带返回的会话 ID 和协商后的协议版本。
`initialize` 会采用请求中的 `2025-11-25` 或 `2025-06-18`。请求其他版本时，响应使用 `2025-11-25`。
初始化结果包含 `instructions`，说明搜索、网页读取和文本查找的使用流程。
`serverInfo.icons` 包含内嵌 SVG data URI，两个 2025 版本的 HTTP 和 stdio 使用相同资源，不依赖管理监听器或额外图片请求。支持图标元数据的客户端可直接显示，不支持该字段的旧客户端可以忽略它。
如果 `MCP-Protocol-Version` 头的值不受支持，服务会返回 HTTP 400 和 JSON 错误响应体，便于较新的客户端回退到
`initialize`。发送 `notifications/initialized` 后即可调用工具。DELETE 结束会话。
携带有效会话的 GET 返回 405，失效会话返回 404，不支持的协议头返回 400。取消请求会停止相应操作，并保留其他仍在等待同一次合并执行的请求。

2025 版本的 `tools/list` 按固定顺序返回工具，包含 `name`、`title`、`description`、`annotations` 和
`inputSchema`。所有工具都是只读的。`web_search` 和 `web_open` 的 `openWorldHint` 为 `true`，
只读取本地状态的 `web_find` 和 `web_search_status` 为 `false`。

HTTP 会话空闲 30 分钟后过期。服务最多保留 100 个会话，每个客户端身份最多保留 20 个。
会话达到容量上限时，服务只会回收同一请求方最早进入空闲状态且没有待处理请求的会话。
没有符合条件的会话时，初始化返回 503。请求历史最多保留 4,096 个 ID，大小不超过 1 MiB。
会话过期或被回收后，请求返回 404，客户端需要重新初始化。

Stdio 会话与子进程的生命周期一致。服务保留有限数量的近期请求 ID 和所有进行中的请求 ID，
整段会话的请求总数不设配额。客户端在整个会话中必须使用唯一的请求 ID。
服务忽略空行，接受 CRLF 换行和开头的 UTF-8 BOM。超过 1 MiB 的行会被丢弃，服务返回带 `id: null` 的
Invalid Request 后继续处理请求。服务读入工具请求后即可按 ID 取消，即使工作线程尚未开始执行。服务忽略与在途请求无关的取消通知。

<a name="settings-status"></a>

## 设置状态

`web_search_status` 返回本地设置及最近一次已知的搜索来源状态。
为此新增的 `settings` 对象包含 `search_enabled`、`open_enabled`、`search_without_asking`、
`provider`、`provider_ready` 和 `revision`。搜索和网页读取开关同时适用于 MCP HTTP 和 stdio。
`web_find` 与状态查询始终可用，切换开关后工具列表保持原样。
`search_without_asking` 为旧客户端保留。当前 QGIS 插件透明转发 MCP 请求，不读取该字段来增加搜索审批。

<a name="administration"></a>

## 管理接口

管理界面使用配置的 Origin，路径为 `/settings/`。默认 Origin 为 `http://127.0.0.1:3183`，
安装版可能使用其部署配置中记录的其他端口。通过该版本的可执行文件运行 `--SettingsUrl`，可获取登录链接。
管理员令牌和业务监听器使用的客户端 Bearer 凭据彼此独立。

管理页分为“访问授权”“搜索设置”“服务状态”。授权的三步流程依次为填写与生成、保存并生效、复制连接资料。只有第二步提交成功后才进入连接资料复制步骤。已保存客户端的详情默认隐藏授权码，管理员可按需显示或复制。切换客户端或标签页、刷新、注销、删除或替换时清空页面原文，之后可通过受保护的单条读取接口再次取得。

| `/admin/api/v1` 下的请求方法与路径 | 鉴权与行为 |
| --- | --- |
| `POST /login-links` | 发送管理员 Bearer 令牌，省略 Origin 和 Cookie。返回一次性登录链接的 `url` 和 `expires_in`。 |
| `POST /session` | 发送 `code` 或 `token`，二选一。设置会话 Cookie，并返回 `csrf`。 |
| `GET /session`、`DELETE /session` | 读取会话的 CSRF 令牌或退出登录。 |
| `GET /settings`、`PUT /settings` | 读取当前设置，或携带之前读取的修订号提交设置补丁。修订冲突时返回 409。 |
| `GET /clients` | 返回客户端授权的 `revision`、`clients` 和 `pending_restart`。每个客户端包含 `id`、`source` 和 `can_reveal`，不包含原文或摘要。 |
| `POST /clients/reveal` | 发送 `id` 和 `revision`，要求管理会话及 CSRF。返回单条 `id`、`token`、`revision` 和 `active`，响应禁止缓存。 |
| `POST /clients` | 发送 `revision`、`id` 和 `token`，添加客户端授权，立即生效。 |
| `PUT /clients` | 发送 `revision`、`id` 和 `token`，替换已有客户端授权码并保留缓存身份。 |
| `DELETE /clients` | 发送 `revision` 和 `id`，删除该客户端授权。重新创建同名客户端不会继承旧缓存身份。 |
| `PUT /clients/{id}`、`DELETE /clients/{id}` | 保留旧客户端兼容接口。ID 来自路径，请求体分别包含 `revision` 和 `token`，或仅包含 `revision`。 |
| `PUT /providers/{id}/key`、`DELETE /providers/{id}/key` | 保存或删除 `key`。响应通过 `key_set` 表示密钥是否已设置。 |
| `POST /providers/{id}/test` | 测试所选来源。返回是否成功、实际来源、结果数量、耗时和最多三条示例结果。 |
| `GET /status` | 返回监听地址、供客户端复制的 `mcp_url`、版本、搜索来源冷却状态和设置错误。 |

管理 API 会校验 Host 和 Origin。登录后的写操作要求提供会话 Cookie、JSON Content-Type 和 `X-QCWS-CSRF`。
首次登录使用允许的 Origin 和 JSON 请求体，此时尚未签发会话的 CSRF 令牌。
登录码通过 URL 片段传递，管理界面使用后会清除该片段。

客户端授权的写操作使用与设置相同的会话和 CSRF 校验，返回更新后的授权列表。列表的 `source` 为 `manual`、`file` 或 `environment`，不会返回原始令牌、令牌摘要或私有来源路径。手动授权码是 1 至 8192 个可见 ASCII 字符，客户端 ID 是 1 至 64 个英文字母、数字、点、下划线或连字符。修订不匹配时返回 409，避免覆盖并发修改。`pending_restart` 表示磁盘授权尚未加载，读取最新修订后再通过网页保存，会将该完整授权列表立即应用，包括先前 CLI 写入的变更，并清除此标记。网页成功响应表示持久化和运行时授权均已更新。

`can_reveal` 表示记录具有可读取来源，不保证外部文件或环境变量当前可读。`active` 比较取得的授权码及客户端身份与运行快照，防止把 CLI 待重启的值或外部已改文件误认为当前生效值。回显失败使用明确原因，不修改授权状态。旧版本仅保存摘要的记录可继续认证，但没有可还原原文，回显会明确失败，本版本不自动迁移或重置它们。

管理界面按受控原因和操作上下文显示中英文提示，不展示任意后端异常或上游响应正文。重复授权码保留 HTTP 400，原因及安全的冲突客户端标识说明应更换哪个输入。无结构化正文的 HTTP 409 不会被推断为修订冲突。网络连接失败、无效 JSON 和未知服务错误有各自提示，未知错误不推断服务没有运行。

缓存的具体原因包括 `instance_already_running`、`cache_locked`、`cache_lock_permission_denied`、`cache_lock_failed`、`cache_path_unavailable`、`cache_capacity_exceeded`、`cache_database_open_failed`、`cache_database_corrupt`、`cache_database_incompatible`、`cache_database_busy`、`cache_storage_full`、`cache_database_error` 和 `cache_worker_unavailable`。临时锁占用或数据库忙可稍后重试，权限、损坏、容量及格式问题需要先修复。持锁 PID、绝对路径和本机诊断只写启动 stderr，不进入公共 MCP 错误响应。

新的客户端应使用请求体传递 `id` 的 `/clients` 接口，包括合法 ID `.` 和 `..`。浏览器会归一化 URL 中的点路径段，因此不能通过旧路径接口可靠地管理这两个 ID。

请求与响应的 JSON Schema 及错误码见 [contracts/admin/v1](../contracts/admin/v1)。

`mcp_url` 优先使用运行配置中的 `business.public_url`，新生成配置默认写入 `https://web-search.vastarmorai.com/mcp`。该字段必须是 HTTPS 地址，路径恰为 `/mcp`，不能包含用户信息、查询或片段。省略时返回实际业务监听器的 HTTP 或 HTTPS 地址，包括绑定端口和 IPv6 方括号。该值只是连接地址元数据，不会根据 Host 白名单推断公网地址，也不会进行网络探测、创建路由或改变授权、Host 和 Origin 格式校验。显示公网地址不代表该实例已经部署在公网。
