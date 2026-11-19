<a name="version-1-interfaces"></a>

# 版本 1 接口

所有业务端点均要求提供 `Authorization: Bearer <client-token>`。
`Bearer` 鉴权方案名不区分大小写。每个客户端都有独立身份。请求的 Host 和浏览器的 Origin 必须与配置项完全匹配。
原生客户端可以省略 Origin。凭据通过请求头传递。远程客户端使用 HTTPS 并验证服务端证书。

| 接口 | 请求 |
| --- | --- |
| REST 搜索 | `POST /v1/search`，请求体为 `web_search` 输入对象 |
| REST 网页读取 | `POST /v1/open`，请求体为 `web_open` 输入对象 |
| REST 文本查找 | `POST /v1/find`，请求体为 `web_find` 输入对象 |
| REST 状态查询 | `GET /v1/status` |
| MCP HTTP | 向 `/mcp` 发送 JSON-RPC POST 请求，协议版本为 `2025-11-25` 或 `2025-06-18` |
| MCP stdio | `--Stdio --ConfigPath FILE`，每行传输一条 JSON-RPC 消息 |

输入 JSON Schema 位于 `contracts/v1`。`contracts/v1/tools.json` 包含各工具的标题、描述和注解，以及 MCP
`instructions` 文本。MCP `tools/list` 直接发布这些文件定义的内容。为兼容会拒绝顶层
`oneOf`、`anyOf`、`allOf` 或 `not` 的模型 API，输入 Schema 使用普通的 Draft 7 对象。
QGIS 适配器内置这五个文件的相同副本，自行检查 `web_open` 的 `url` 和 `page_id` 是否恰好提供一项，并在向指定的
REST 后端发送请求前校验输入。

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
`utm_*`、`gclid` 和 `fbclid` 参数。多个结果命中同一个去重键时，保留最先返回的 URL。

`time_range` 接受 `any`、`day`、`week`、`month` 和 `year`。这是传给上游的时间提示，
结果的发布日期可能超出该范围。每条结果包含 `title`、`url`、`snippet`、`sources` 和
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
| 完整 REST 或 MCP 响应 | 1 MiB |

只有超出传输限制时才返回 `response_too_large`。输入超出其他读取、解析、遍历或文本限制时，
服务保留可读部分并设置 `truncated`。为满足负载大小限制，网页读取和文本查找可能减少返回的行和链接。
此时可用 `next_start_line` 接着读取省略的正文行或匹配行。

<a name="results-and-errors"></a>

## 结果与错误

工具成功返回的负载包含 `contract_version: 1`。增加字段时保留该版本号。
MCP 将相同的负载分别放在 `structuredContent` 和一个 JSON 文本内容块中。
业务失败或参数违反输入 Schema 时，响应使用 `isError: true`，并附上本节所示的错误对象，便于模型修正调用。
错误消息指出第一个未通过校验的字段，例如
`Input does not match the schema at /limit: instance exceeds maximum of 20.`。
REST 以 HTTP 400 返回同样的消息。
只有调用未知工具名称时，才返回 JSON-RPC 参数无效错误。此版本不声明 `outputSchema`。

JSON 输入上限为 1 MiB，容器最大嵌套深度为 64。服务在构建 JSON 树前拒绝嵌套过深的输入。
序列化请求 ID 的上限为 64 KiB，包含 JSON 引号和转义。超大的 ID 会收到带 `id: null` 的 Invalid Request，
不创建会话，也不执行工具。客户端必须使用符合限制的 ID，才能将响应和请求对应起来。
对于已接受的请求 ID，服务会在响应中保留该 ID。如果完整响应超出 1 MiB，则返回 JSON-RPC 错误。
Stdio 响应也受 1 MiB 上限约束。队列最多容纳 32 条消息，占用不超过 4 MiB，正在写入的消息也计入。
关闭 stdin 后，服务会等待短暂的输出排空期，再取消阻塞的 stdout 写入。即使客户端停止读取，进程也能退出。

REST 错误返回对应的 HTTP 状态码，以及以下格式的响应体。

```json
{"error":{"code":"busy","message":"Service capacity is exhausted.","retryable":true}}
```

错误响应不包含成功结果的 `contract_version` 字段。客户端应先依据 HTTP 状态码或 MCP 错误类型判断请求是否成功，
再解析成功响应的内容。`retryable` 指示稍后重试同一调用是否可能成功。HTTP 429 响应还带有 `Retry-After`
响应头。上游限流时，错误对象可以包含与该头对应的 `retry_after_seconds` 字段。

| 错误码 | 常见 HTTP 状态码 | 可重试 | 含义 |
| --- | --- | --- | --- |
| `invalid_argument` | 400 | 否 | 输入违反对应版本的 Schema，或未在 `url` 与 `page_id` 之间恰好选择一项 |
| `unauthorized` | 401 | 否 | 缺少客户端的独立凭据，或提供的凭据无效 |
| `forbidden` | 403 | 否 | 请求的 Host 或 Origin 不在配置的允许列表中 |
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
| `cache_unavailable` | 503 | 是 | 无法安全使用私有缓存 |
| `timeout` | 504 | 是 | 操作超出时限 |

MCP HTTP 先进行初始化，随后在请求头中携带返回的会话 ID 和协商后的协议版本。
`initialize` 会采用请求中的 `2025-11-25` 或 `2025-06-18`。请求其他版本时，响应使用 `2025-11-25`。
初始化结果包含 `instructions`，说明搜索、网页读取和文本查找的使用流程。
如果 `MCP-Protocol-Version` 头的值不受支持，服务会返回 HTTP 400 和 REST 错误响应体，便于较新的客户端回退到
`initialize`。发送 `notifications/initialized` 后即可调用工具。DELETE 结束会话。
GET 返回 405，因为服务没有用于推送消息的 SSE 流。取消请求会停止相应操作，并保留其他仍在等待同一次合并执行的请求。

`tools/list` 按固定顺序返回工具，包含 `name`、`title`、`description`、`annotations` 和
`inputSchema`。所有工具都是只读的。`web_search` 和 `web_open` 的 `openWorldHint` 为 `true`，
只读取本地状态的 `web_find` 和 `web_search_status` 为 `false`。

HTTP 会话空闲 30 分钟后过期。服务最多保留 100 个会话，每个客户端身份最多保留 20 个。
会话达到容量上限时，服务只会回收同一请求方最早进入空闲状态且没有待处理请求的会话。
没有符合条件的会话时，初始化返回 503。请求历史最多保留 4,096 个 ID，大小不超过 1 MiB。
会话过期或被回收后，请求返回 404，客户端需要重新初始化。

Stdio 会话与子进程的生命周期一致。服务保留有限数量的近期请求 ID 和所有进行中的请求 ID，
整段会话的请求总数不设配额。客户端在整个会话中必须使用唯一的请求 ID。
服务忽略空行，接受 CRLF 换行和开头的 UTF-8 BOM。超过 1 MiB 的行会被丢弃，服务返回带 `id: null` 的
Invalid Request 后继续处理请求。如果取消消息刚好在对应请求开始前到达，服务仍会取消该请求。

<a name="settings-status"></a>

## 设置状态

`web_search_status` 和 `GET /v1/status` 返回本地设置及最近一次已知的搜索来源状态。
为此新增的 `settings` 对象包含 `search_enabled`、`open_enabled`、`search_without_asking`、
`provider`、`provider_ready` 和 `revision`。搜索和网页读取开关同时适用于 REST、MCP HTTP 和 stdio。
`web_find` 与状态查询始终可用，切换开关后工具列表保持原样。
QGIS 适配器根据 `search_without_asking` 判断是否在当前会话中请求确认。

<a name="administration"></a>

## 管理接口

管理界面使用配置的 Origin，路径为 `/settings/`。默认 Origin 为 `http://127.0.0.1:8091`，
安装版可能使用其部署配置中记录的其他端口。通过该版本的可执行文件运行 `--SettingsUrl`，可获取登录链接。
管理员令牌和业务监听器使用的客户端 Bearer 凭据彼此独立。

| `/admin/api/v1` 下的请求方法与路径 | 鉴权与行为 |
| --- | --- |
| `POST /login-links` | 发送管理员 Bearer 令牌，省略 Origin 和 Cookie。返回一次性登录链接的 `url` 和 `expires_in`。 |
| `POST /session` | 发送 `code` 或 `token`，二选一。设置会话 Cookie，并返回 `csrf`。 |
| `GET /session`、`DELETE /session` | 读取会话的 CSRF 令牌或退出登录。 |
| `GET /settings`、`PUT /settings` | 读取当前设置，或携带之前读取的修订号提交设置补丁。修订冲突时返回 409。 |
| `PUT /providers/{id}/key`、`DELETE /providers/{id}/key` | 保存或删除 `key`。响应通过 `key_set` 表示密钥是否已设置。 |
| `POST /providers/{id}/test` | 测试所选来源。返回是否成功、实际来源、结果数量、耗时和最多三条示例结果。 |
| `GET /status` | 返回监听地址、版本、搜索来源冷却状态和设置错误。 |

管理 API 会校验 Host 和 Origin。登录后的写操作要求提供会话 Cookie、JSON Content-Type 和 `X-QCWS-CSRF`。
首次登录使用允许的 Origin 和 JSON 请求体，此时尚未签发会话的 CSRF 令牌。
登录码通过 URL 片段传递，管理界面使用后会清除该片段。

请求与响应的 JSON Schema 及错误码见 [contracts/admin/v1](../contracts/admin/v1)。
