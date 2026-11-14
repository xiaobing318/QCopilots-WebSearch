# QCopilots-WebSearch design 1.0

## Scope and accepted decisions

Apache-2.0 C++20 and Qt 6.12.0 service for Windows 11 x64 and Ubuntu 26.04 x64. A separately deployed SearXNG supplies public web results. Public search is best effort, without a commercial quota imposed by this software. No independent index, paid fallback, PDF, JavaScript execution or LLM answer generation is included.

The user approved public publication to xiaobing318/QCopilots-WebSearch, commits and pushes, ZIP/MSI/DEB releases, project-local Claude review configuration, GCC and Docker Compose on Ubuntu, and the official WiX package after being informed of its OSMF terms.

## Architecture and contracts

Qt Core/Network/SQL coordinate the application. cpp-httplib handles bounded HTTP ingress, libcurl handles outbound HTTP(S) with validated DNS pinning, Lexbor parses HTML. Qt HTTP Server is excluded because its open-source license is GPL rather than LGPL. Qt is dynamically linked and its corresponding source and replacement instructions accompany releases.

REST /v1/search, /v1/open, /v1/find and /v1/status share one service with MCP /mcp and stdio. MCP targets 2025-06-18, uses content plus structuredContent, and distinguishes JSON-RPC protocol errors from tool execution errors.

- web_search: query, limit=10 (1..20), language=all, time_range=any/day/month/year, domains=[]. Domains are normalized with IDNA and applied as strict hostname or subdomain output filters. Time is explicitly an upstream hint. Provider redirects are rejected.
- web_open: exactly one of url/page_id, start_line=1, max_lines=200 (1..500). HTML/plain text only. Return title, final URL, page_id, numbered lines, links, fetch time and truncation. Page IDs are opaque and scoped to the authenticated principal.
- web_find: page_id, literal pattern, case_sensitive=false, max_matches=20 (1..100). Unicode case-insensitive matching by default. Unknown or expired pages return page_unavailable.
- web_search_status: local service and dependency state, without triggering a public search.

Errors include invalid_argument, unauthorized, forbidden, busy, timeout, provider_unavailable, backend_unavailable, page_unavailable, unsupported_content and response_too_large. Schema fixtures are the source of truth across REST and both MCP adapters.

## Security, capacity and lifecycle

Default loopback binding, independent bearer credentials per client, exact Origin allowlist, remote TLS reverse proxy. No OAuth/SSO claim. Credentials use environment variables or protected files, never tool arguments. No token, query or page bodies in logs. Health/metrics have separate control capacity.

Five concurrent business calls, immediate busy/429 on overload, bounded workers. Coalesced calls retain independent cancellation. Search deadline 15 s, open deadline 20 s, QGIS backend deadline 25 s, shutdown within 10 s. Search TTL 300 s, page TTL 1800 s, SQLite cache at most 256 MiB and 10000 entries. A dedicated Qt thread owns each cache database connection.

Public URL fetching only permits HTTP(S) global addresses. Validate every resolution and redirect, pin validated IPs with libcurl while preserving TLS identity, reject userinfo and internal/metadata destinations, do not inherit uncontrolled system proxies. Limit request bodies to 1 MiB, both compressed and decoded bodies to 5 MiB, and redirects to five within one total deadline. The fixed administrator SearXNG endpoint has a separate trusted-backend policy. Distinguish empty success, partial results and total provider failure.

## Integration and deployment

Add a GPL Python qcopilots_mcp_server_web_search plugin to QGIS, reusing the existing service wrapper and local MCP server. It calls fixed REST routes, does not launch or stop the independently managed native service, and uses a backend credential separate from the local MCP credential. Local health and tools/list remain available during backend outages. Preserve formal_restricted network policy and existing manager startup lists. QGIS baseline is 84b72dce7a3a66f6b9ac968ce60460d6b498b892 on feature/final-4_2_0-qcopilots.

Ubuntu reference deployment: systemd native service on loopback, Caddy in Compose host networking, SearXNG published only to host loopback, Valkey private. External access uses authenticated HTTPS. Windows needs no Docker and can use a configured protected remote SearXNG or connect its QGIS adapter to the shared native service. Test CAs are trusted only by explicit test clients.

## Build and distribution

PowerShell/Bash entrypoints share --ConfigPath, --Profile, --Stages, --CheckOnly, --DryRun. Stages doctor,bootstrap,configure,build,test,install,publish. Windows bootstrap installs only Qt and diagnoses other tools. Ubuntu bootstrap installs GCC/CMake/Ninja/Git/Python and dependencies plus pinned Qt. Build/runtime configs have JSON Schema, and each field explains purpose, choices and effects. All paths are configurable. Downloads are pinned, hashed and cached.

WiX 7.0.1 with matching extensions, .NET 10 LTS and CPack modern WiX mode must pass a small MSI build/install PoC before release. Use standard MSI service and upgrade tables, preserve configuration/data, verify failed-upgrade rollback separately from operational downgrade. No certificate purchase is authorized. Unsigned packages are labelled accordingly.

PR/push CI runs windows-2022 and ubuntu-26.04. Version tags build all artifacts from the same commit. Release only after complete ZIP/MSI/DEB, tests, SBOM, checksums and matching Qt source. Windows Server CI does not substitute Windows 11 acceptance. Qt upstream does not currently list Ubuntu 26.04, so project support requires actual testing.

## Acceptance and review

Deterministic tests use temporary directories, dynamically allocated ports, fake upstreams and injectable clocks. Cover encoding, filters, page identity, truncation, deadlines, TLS, Origin, DNS rebinding/redirects, compression, cancellation, cache limits and protocol lifecycle. A barrier test holds five requests, rejects the sixth and verifies health and recovered capacity.

On >=2 vCPU/4 GiB, a 100 ms fake upstream supports five clients at total 2 requests/s for 30 min cold and 30 min warm. Normal success 100%, p95 <=1 s, RSS <=512 MiB, health p95 <=200 ms, shutdown <=10 s. Faults must produce the expected bounded errors. Public checks use 60 fixed semantic queries (30 Chinese/30 English), spaced >=2 s, report success/latency/engine limitations, and complete one real search-open-find flow. No public performance SLA or public stress test.

Verify install/upgrade/uninstall/config preservation/rollback on real Windows 11 and Ubuntu 26.04, and portable ZIP without development dependencies. Run the user's exact QGIS BuildPipeline commands without clean. Browser verification records and restores Vastarmor AI and MCP configuration. Only processes started for this work are stopped.

Council reviews use two actual read-only Codex agents and actual cc_adversarial_review, same frozen diff, independent first pass, max three complete rounds. Claude target Opus 5.5/xhigh must be verified from actual execution. Report missing seats, tests or environment evidence as incomplete, never as a pass. Current implementation baseline is empty and no platform acceptance has run.

## Implementation decisions and milestone gates (revision 2)

The main implementation agent owns all repository edits and validation. Read-only agents independently review or propose code. M0 verifies pinned Qt downloads on each platform, actual-address URL blocking, and MSI packaging. M1 implements configuration, cache, REST search and controlled upstream tests. M2 adds extraction/find and MCP HTTP/stdio. M3 integrates and tests QGIS. M4 performs installed-package lifecycle, long runs, live queries, browser acceptance and final council review. M5 publishes only the complete verified artifact set. A failed prerequisite stops its dependent milestone, while independent work continues. No fixed calendar estimate is represented as a guarantee.

The Windows Qt and WiX M0 probe has now actually built and run Qt 6.12.0 with SQLite, generated ZIP/MSI, installed the per-user MSI, run the installed executable and uninstalled it successfully on Windows 11 (all exit codes zero). This proves the tooling combination, not production service installation or rollback. aqt 3.3.0 cannot locate the new Windows Qt metadata layout. Bootstrap therefore downloads hash-locked official archives, extracts their root into the explicit version/architecture directory, and uses the pinned aqt relocation helper. It does not require a Qt account. Linux archive and source hashes are independently recorded in dependency locks and still need runtime validation.

### Process, thread and identity model

HTTP service instances share one cache thread and one global five-call gate. Each stdio process is a complete isolated instance with a private temporary cache directory and a random local principal. Its five-call cap is explicitly per process. Stdio processes never use the persistent HTTP cache or expose HTTP listeners. EOF cancels work and deletes only their owned temporary data. Two concurrent stdio instances are tested. stdout is binary UTF-8 protocol only, logging uses stderr.

Each HTTP request has its own libcurl multi/easy handles with c-ares asynchronous DNS. A 50 ms polling cycle and XFERINFO cancellation check enforce one end-to-end deadline. No network or SQL operation holds the job registry lock. Search coalescing keys include principal, provider identity and normalized arguments. Each waiter has its own cancellation/deadline. The upstream is cancelled only after the last waiter leaves or the shared deadline expires, and its resource slot is held until the transfer has actually ended.

Shutdown stops business admission, cancels jobs, wakes waiters, joins network workers, drains or cancels bounded cache commands, closes the database on its owning thread and then joins that thread. Never terminate a Qt thread forcibly. The cache object's owner and close operation serialize access so no BlockingQueuedConnection can target a stopped event loop.

### Explicit outbound and ingress policy

Public fetch sets CURLOPT_PROXY to an empty string and CURLOPT_NOPROXY to '*'. No public-fetch proxy option exists in v1. Backend connections have a separate fixed-admin-address method and also do not inherit system proxies. HTTP(S) protocol allowlists, disabled netrc/cookies/Alt-Svc and manual redirects prevent alternative resolution paths. OPENSOCKETFUNCTION validates each actual sockaddr before connect, and that same sockaddr is the connection target. This connection-level pinning avoids a separate DNS validation/re-resolution race while preserving URL-based TLS verification.

IPv4 must exclude unspecified/reserved, loopback, RFC1918, link-local, CGNAT, documentation, benchmarking, multicast and future-use ranges, including 0/8, 100.64/10, 192.0.0/24, 192.88.99/24, 198.18/15 and 240/4. IPv4-mapped IPv6 is validated as IPv4. IPv6 is restricted to global unicast with exclusions for documentation, Teredo, 6to4, NAT64 and other special-purpose ranges. Scope IDs are rejected. Tests cover numeric/hexadecimal hosts, all denied ranges, DNS alternation and public-to-private redirect chains. HTTPS-to-HTTP redirects are rejected.

Health/live, health/ready and metrics use a separate loopback control listener with a separate bounded thread pool. They require bearer authentication, and no endpoint trusts source-address loopback as identity. Host and supplied Origin are validated. Caddy only exposes /v1/* and /mcp, never the control listener. Configure bounded connection queues, header/body timeouts, low keep-alive counts and idle timeouts. Test 50 idle/slow clients as well as the five-call barrier. The five-call gate is global and does not promise fairness or independent tenant quotas in v1.

Windows runs the production service as its virtual service account with specific configuration/state ACLs. Ubuntu uses a dedicated unprivileged account with NoNewPrivileges, ProtectSystem=strict, PrivateTmp, restricted address families and explicit writable state paths. These measures complement application address checks. Production config and credential files live outside the package-owned program tree. Token comparison is constant-time over hashes, minimum generated entropy is 256 bits. Explicit config reload/restart rotates credentials. Seamless hot rotation and enterprise multi-tenancy are outside v1.

### Extraction, protocol and deployment details

Extraction is iterative, skips script/style/nav/header/footer/template/noscript and hidden content, and prefers main/article before body. Paragraph boundaries produce lines, whitespace is collapsed, long lines wrap at 1000 Unicode characters. Limit extracted text to 200000 characters, lines to 10000, links to 200, and each wire response to 1 MiB. Mark every truncation. Bound HTML parsing input to 1 MiB and markup tokens to 50000, and reject pathologically deep traversal above 512. Charset selection uses BOM, HTTP charset, an early HTML meta declaration, then UTF-8, with explicit unsupported-encoding errors. Full browser WHATWG decoding and arbitrary legacy encodings are not claimed. Find uses Qt's Unicode case-insensitive comparison and returns line matches rather than ambiguous byte offsets.

Supply site: hints to SearXNG as well as strict output domain checks. Fetch at most three upstream pages within the same deadline to fill a requested count, return fewer when exhausted, and never relax the output filter. SearXNG receives POST form bodies, has JSON enabled, is private, and its limiter is configured for the trusted native backend. Disable access query/body logging. Pin deployment images by digest and report unresponsive_engines separately from process readiness.

MCP sessions are unpredictable, identity-bound, capped and expire after 30 minutes. Initialize negotiates 2025-06-18, tools require notifications/initialized, notification/response POSTs produce 202 without JSON, GET returns 405, DELETE ends a session. POST requires JSON and Accept supporting JSON/SSE. CORS preflight is independent of bearer validation and exposes Mcp-Session-Id. Reject batch and invalid IDs, preserve integer/string ID types, support MCP-Protocol-Version and scoped cancellation. Cancelled calls end their HTTP request without a JSON-RPC result. Static Bearer clients and stdio are supported, OAuth-only remote clients are not claimed.

REST maps invalid_argument to 400, unauthorized to 401, forbidden/destination_blocked to 403, page_unavailable to 404, unsupported_content to 415, busy/rate_limited to 429 with Retry-After, request_too_large to 413, response_too_large to 502, provider_unavailable/upstream_http_error to 502, timeout to 504 and unexpected internal failures to 500 with a fixed safe message. MCP tool failures remain HTTP 200 with isError and the same structured code. Protocol errors remain JSON-RPC errors. Status includes contract_version=1. The QGIS adapter validates version on status, applies its existing formal_restricted policy, bypasses system proxies and does not request backend state while listing tools.

SQLite uses DELETE journaling, in-memory temp storage and full auto-vacuum. The database page cap reserves space for rollback journals within the 256 MiB overall cache budget. The cache is disposable, schema changes use a fresh versioned database, and packages do not preserve a cache as user content. Persistent configuration remains schema_version=1 for v1 upgrades, with backups before any explicit future migration and no automatic downgrade of incompatible schemas.

Linux CI additionally runs ASan/UBSan, static analysis and bounded libFuzzer smoke targets for HTML, URL and JSON-RPC parsing with checked-in non-sensitive corpus inputs. Test deep markup, long lines, maximum concurrent page loads, cancellation and memory peaks. Keep the agreed 2 requests/s long runs for stability and add zero-think-time five-client bursts for capacity. Public search remains explicitly informational except for one actual successful end-to-end flow, as the user requested.

Release verification binds Windows 11 acceptance to the exact commit and package SHA-256, and refuses missing/stale evidence. GitHub artifact attestations supplement checksums without claiming Authenticode signing. Production Windows service installation, Ubuntu tests, final packages and all other acceptance checks remain pending. Never run a QGIS clean stage or release only a subset of ZIP/MSI/DEB to bypass these gates.

## Council R2 arbitration and final design clarifications

R2-01 accepted: a tag workflow builds candidate packages once from a clean checkout. Windows 11 and Ubuntu 26.04 acceptance consume those exact downloaded bytes and record commit, workflow run and SHA-256. A separate promotion workflow verifies those records and publishes the same retained artifacts without rebuilding. Local packages are development evidence only. Installed QGIS adapter files are compared with source hashes after the prescribed no-clean pipeline.

R2-02 accepted in scope: the disposable per-user probe has a distinct product identity and was uninstalled. Production MSI is always per-machine with a fixed UpgradeCode. ServiceInstall/ServiceControl, virtual-account ACLs and a failed-upgrade rollback probe are the next packaging prerequisite before production service integration is considered proved. Implementation drafts may progress independently while that prerequisite is being tested. Production packages must never upgrade the probe product.

R2-03 accepted: DEB rollback means reinstalling a retained previous DEB and recovering the previous working service, not dpkg transactional rollback. Package only a config example. Maintainer scripts create user-owned config only when absent, preserve it on upgrades and normal removal, and explicitly handle abort-upgrade. v1.0 rejects unknown configuration keys. Future versions may not claim backward downgrade compatibility if they add fields without providing a tested migration/backup path. No automatic schema downgrade is performed.

R2-04 accepted: client disconnect is a cancellation source, checked at most every 50 ms alongside transfer cancellation. cpp-httplib 0.66 exposes Request::is_connection_closed. Verify abandoned local requests release resources promptly, including a <=200 ms healthy-OS test. Bound the connection pool and queue, total header bytes and per-read timeouts. Native loopback ingress does not claim a total request-header deadline against a local trickle attacker. Remote ingress requires Caddy read_header timeout, connection limits and TLS. Slow-client tests cover both idle and trickle connections and verify control-listener availability.

R2-05 accepted with protocol-preserving resolution: keep both serialized JSON TextContent and structuredContent for MCP interoperability. Bound service payloads to 240 KiB and verify the final REST/MCP envelope is below 1 MiB. Open/find truncate lines and links to fit, mark truncated and return next_start_line when more lines remain. Search bounds every upstream title/snippet/URL and metadata collection. request_too_large denotes inbound 413, response_too_large denotes excessive upstream transfer/extraction input. Complete error fixtures include destination_blocked, rate_limited, upstream_http_error, cache_unavailable and cancelled. The 5 MiB transfer cap and 1 MiB HTML parser cap are deliberate separate resource budgets, with parser truncation disclosed. Stopping at the parser cap is optional and must not silently corrupt an encoding boundary.

R2-06 accepted: use the already-linked Lexbor encoding module for identical supported encodings on both platforms, including UTF-8/UTF-16, Windows-1252, GBK/GB18030 and Big5. Keep explicit unsupported/invalid encoding failures and BOM > HTTP > early meta > UTF-8 selection. Tests must cover Chinese legacy encodings.

R2-07 accepted: GCC remains the production Ubuntu compiler. Clang/libFuzzer and clang-tidy are CI-only analysis tools, installed on the disposable runner if absent. They are not installed by the ordinary user bootstrap. R2-08 accepted: byte fixtures bypass EOL conversion, PowerShell wrappers contain only ASCII, private PEM/certificate files are ignored, and TLS test credentials are generated in temporary directories.

R2-09 partially accepted: exclusively lock each cache database with QLockFile, fail clearly on another owner, check corruption and test FULL/BUSY behavior. Cache failures must not be represented as successful empty results. Only the worker owns SQL, and no caller may leave a queued callback holding references to its stack. A permanently stalled OS filesystem cannot be given an unconditional 10-second shutdown guarantee by C++ futures. The SLA is tested on healthy local storage, and deployment requires local cache storage. A killed process may leave disposable temp files, so document cleanup and use owner-only temporary directories. Memory-only stdio storage is an optional implementation simplification, not a prerequisite for isolation.

R2-10 accepted: status reads a cached local snapshot without creating outbound requests and bypasses business admission. Sessions have a global cap of 100 and a per-principal cap of 20, with 30-minute idle expiry. In-flight sessions are not evicted to admit another client. R2-11 accepted: all persistent search/page keys include principal, provider identity and contract version. Do not join a shared search with less than half of a fresh search deadline remaining. Admission is released before serializing/writing the response. Verify immediate follow-up requests and independent waiter cancellation. Per-process capacity and the absence of tenant fairness remain explicit.

These clarifications resolve design decisions only. The third council round judges whether implementation may proceed under the recorded gates. It does not grant a release pass before actual production packaging, cross-platform, long-run and QGIS acceptance evidence exists.

## Implementation arbitration following final design review

Excessive upstream transfer or extraction complexity returns response_too_large / HTTP 502. Only an excessive inbound request uses request_too_large / HTTP 413. A completed transfer at most 5 MiB can still produce a disclosed, truncated parser result at the independent 1 MiB parser budget. Cache failures use 503 and cancellation uses 499 for REST. MCP preserves the same structured error code.

At a session capacity boundary the server evicts only the oldest idle session owned by the requesting principal. It never evicts another principal or an in-flight session. When no eligible session exists initialization returns 503.

HTTP session history is limited to 4,096 request IDs / 1 MiB and clients can initialize a new session after retirement. Stdio session lifetime is the child process lifetime. Its receiver keeps a bounded recent ID window plus every active ID, without a lifetime request quota. Senders must still use unique IDs throughout a session. Stdio explicitly converts rejected requests into JSON-RPC errors because it has no HTTP status channel.

The proposed encoding-replacement extension is deferred. This version deliberately rejects malformed input with unsupported_content and documents that policy rather than silently substituting characters. Actual byte fixtures verify the accepted UTF, Chinese and legacy encodings. No outputSchema is advertised for tools in this version, so successful and failed structuredContent are not incorrectly constrained by a success-only schema.
