# Runtime architecture

REST, MCP HTTP and stdio share the service and versioned tool contracts. Business, control and admin HTTP listeners each have a bounded worker pool. The business gate is shared by REST and MCP HTTP. A stdio process has its own service instance, random identity and private temporary cache. Closing stdin cancels that instance's work and removes its temporary state. Protocol output goes to stdout and logs go to stderr.

A dedicated Qt thread owns each SQLite connection. Search cache keys and coalesced work include the client identity, effective source configuration and cache layout version. Page keys include the client identity and stay valid when the source changes. Identical requests can join a search while at least half of a fresh search deadline remains. Waiters have independent deadlines, and the upstream stops when the last waiter leaves or the shared deadline expires. The upstream ends up to a second before its waiters so earlier SearXNG results can still be returned as `partial`. The service delivers the result before an asynchronous search-cache write.

## Settings and sources

Deployment settings take effect on restart. An MSI or DEB service loads `data/runtime.json` beside its executable. A portable process uses the same default or an explicit configuration path. The `project_version` field ties the runtime file to the executable's release. User settings, provider keys, credentials and cache belong to that deployment. The settings store publishes immutable settings and credential snapshots. The admin API writes a validated update atomically and detects mismatched revisions. Each in-flight operation keeps its starting snapshot. The daemon polls for filesystem changes every two seconds and retains its last valid snapshot when an edit fails validation. A separate protected file holds provider keys.

The settings listener defaults to loopback port 8091. Package initialization records a free port if the default is occupied. Qt resources serve the web app from the service binary. A separate admin credential signs in directly or issues a one-time browser login link. Sessions are held in memory. Host and Origin allowlists, session cookies and CSRF headers govern edits. See [admin API](api.md#administration).

The service checks search and open switches before tool execution. The QGIS adapter applies the separate confirmation preference using its shared host bridge. One authorization covers a QGIS session. Direct MCP and REST clients handle confirmation independently. Status reads local and last-known information without querying a provider.

The provider registry selects DuckDuckGo HTML, DuckDuckGo Lite and Bing fallback in `auto`, or a configured SearXNG, Brave, Tavily, Exa, Perplexity or DeepSeek provider. Search results pass through shared filtering, deduplication and normalization. Auto applies bounded pacing and cooldown. Provider settings and credentials are independent of a client's business token.

## Outbound HTTP and TLS

libcurl uses c-ares to resolve addresses. Public sources and webpage requests check the actual connection address and every redirect. A fixed administrator-configured SearXNG endpoint uses the private backend policy. Runtime traffic ignores system and environment proxies. Build download proxies apply to build tooling.

TLS validates hostnames and certificate chains with Schannel on Windows and distribution OpenSSL on Ubuntu. Schannel uses the Windows trust store by default. A private backend may configure its own CA file. The Windows TLS fixture observed AIA retrieval for missing intermediates. Deploy endpoints with complete certificate chains. Administrators can review the Windows `MaxAIAUrlRetrievalCountPerChain` setting when controlling AIA retrieval. Both platforms' fixtures enforce no online CRL or OCSP requests, and Ubuntu checks that there is no AIA retrieval.

A hash-checked cpp-httplib patch keeps a two-second request-input deadline active through headers, body, chunk framing and trailers. Application work has its own deadlines. Shutdown interrupts accepted sockets, including sockets queued for workers.
