# Team deployment

The reference deployment targets 10–50 team members, with five concurrent
business operations by default. This is a capacity setting, not a promise of
five simultaneous upstream successes. Public engines can return partial results,
CAPTCHAs or throttling. The service never bypasses those restrictions.

## Ubuntu host

Install the release DEB using apt. The package creates the unprivileged
`qcopilots-websearch` account, a random credential and a systemd unit.

```bash
sudo apt install ./qcopilots-websearch_<version>_amd64.deb
sudo systemctl status qcopilots-websearch
```

The configuration is `/etc/qcopilots-websearch/runtime.json`, the generated
credential is `client.token` in that directory and state resides under
`/var/lib/qcopilots-websearch`. The service owns its private token and state.
Configuration is administrator-writable and service-readable. Do not make
token files readable by a group or by other users.

Copy `deploy/team` to an administrator-managed deployment directory. Copy
`.env.example` to `.env`, set a real DNS hostname and ACME contact, and supply a
cryptographically random `SEARXNG_SECRET`. The native `--GenerateToken` command
can generate it. Store the value directly in the private environment file without
printing it in shell or CI logs. Use mode 0600 for that file.

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d
```

Only Caddy uses host networking, which lets it reach the native loopback
listener. SearXNG is published on `127.0.0.1:8888`. Valkey is reachable only on
the internal Compose network. Caddy exposes the public HTTPS API, while its own
liveness check uses loopback 8090. Public DNS and inbound 80/443 are needed for
automatic certificates. No native control endpoint is published by Caddy.

Set the native provider to `http://127.0.0.1:8888/search`. Add the real external
authority to `business.allowed_hosts`, for example `search.example.org` and
`search.example.org:443`. For the QCopilots WebUI, add the exact Origin
`https://chat.vastarmorai.com` to `business.origins`. Retain loopback binding and
the exact local authority needed by local clients. Origins have no trailing slash.

Add a unique client identity and token file for each integration or team user.
Keep the native backend credential separate from a QGIS local MCP credential.
Restart after validating configuration:

```bash
sudo -u qcopilots-websearch /opt/qcopilots-websearch/bin/qcopilots-websearch --ConfigPath /etc/qcopilots-websearch/runtime.json --CheckConfig
sudo systemctl restart qcopilots-websearch
```

Caddy preserves client Authorization, Host and Origin. It does not substitute a
shared gateway token. Do not add `flush_interval -1`, because it changes upstream
cancellation behavior after client disconnect. The native service rejects unknown
Origins and hosts. Keep 8088, 8089, 8888 and 8090 inaccessible from external hosts.

The SearXNG limiter is disabled in this private backend template. SearXNG is not
an independently public endpoint. Exposing it requires a separate authentication,
rate-control and proxy-address design. Fixed container image digests are committed
in `compose.yml`. Upgrade them deliberately and repeat acceptance.

## Windows

The MSI installs a machine service named `QCopilotsWebSearch` under the virtual
account `NT SERVICE\QCopilotsWebSearch`. Program files, shared Qt libraries and
the bundled OpenSSL 3.5.9 DLLs go under Program Files. OpenSSL uses `no-asm`, so
performance evidence applies to that build configuration. Configuration, token
and private state are under
`%ProgramData%\QCopilots-WebSearch`. The service can read its credential and write
its state, but cannot rewrite the configuration. Only administrators and SYSTEM
can change service configuration or credentials.

The MSI needs elevation. Read the Apache and separate Microsoft runtime terms
shown by the installer. The package is unsigned until release signing evidence
states otherwise. Uninstall removes program files and the service while retaining
generated configuration and data. Do not delete retained data unless intentionally
resetting the deployment.

A portable ZIP can be extracted to an arbitrary directory, including paths with
spaces or Unicode. Use its `bin/qcopilots-websearch.exe` and initialize configuration
in a private writable directory. It does not register a service. Windows does not
require Docker. A Windows native daemon still needs a reachable SearXNG JSON API,
which is different from this project's `/v1/search` REST API. Alternatively, a
Windows MCP client can connect directly to the team's Ubuntu HTTPS gateway.

## Runtime trust and proxies

Runtime HTTPS uses the trust sources described in [architecture](architecture.md).
Install the complete server certificate chain at the HTTPS endpoint. The service
does not fetch missing issuers or perform online CRL/OCSP checks. Runtime requests
do not inherit proxy settings. Configure a directly reachable SearXNG endpoint.
The build configuration's proxy affects dependency downloads only.

## QGIS

Install the separate `qcopilots_mcp_server_web_search` plugin from the QGIS branch
(`python/plugins/qcopilots_mcp_server_web_search`). It reuses the existing
QCopilots Manager lifecycle and local MCP HTTP transport. It connects to an
already-running native backend and does not own that backend's process. The
existing seven-service startup preference is not replaced.

### Give QGIS its own credential

The adapter authenticates to the native service as an ordinary REST client.
Give every QGIS user a separate client identity, so that page snapshots and cache
entries stay private to that user.

1. Generate a value with `qcopilots-websearch --GenerateToken`. Keep it out of
   shell history and logs.
2. Store it in a token file beside the service configuration, for example
   `clients/qgis-alice.token`, protected like `client.token`. On Ubuntu the
   file is owned by `qcopilots-websearch` with mode 0600. On Windows only
   administrators, SYSTEM and the service account may read it.
3. Add `{"id": "qgis-alice", "token_file": "clients/qgis-alice.token"}` to
   `clients`, run `--CheckConfig` and restart the service.
4. Give the user the same value in a file only that user can read, or in the
   user environment variable `QCOPILOTS_WEBSEARCH_BACKEND_TOKEN`. On Linux and
   macOS the adapter rejects a token file that other users can read.

A single-user setup created with `--InitializeConfig` already owns its generated
`client.token`. Point the adapter's `auth.token_file` at that absolute path
instead of copying the value.

### Adapter settings

Copy the adapter's configuration example to a private path and select it using
`QCOPILOTS_WEBSEARCH_CONFIG`, or place it at
`<QCOPILOTS_HOME>/web_search/config.json`. Use a dedicated `token_env` or
`token_file`. The existing `formal_restricted` network allowlist must explicitly
permit the backend origin. Container loopback validation is unchanged.

| Adapter setting | Matching service setting |
| --- | --- |
| `backend_url` | Locally exactly `http://127.0.0.1:<business.port>`, and that authority must be listed in `business.allowed_hosts`. Plain HTTP is accepted only for a literal loopback IP, so `localhost` is rejected. `[::1]` is refused because the service binds 127.0.0.1. A remote backend is an HTTPS origin without a path, with certificate verification. |
| `timeout_seconds` | Larger than both `limits.search_timeout_seconds` and `limits.open_timeout_seconds`, plus about five seconds. The defaults 25, 15 and 20 satisfy this. A shorter client deadline reports `timeout` while the service is still working, and every retry starts a new page fetch. |
| `max_concurrency` | At most `limits.max_concurrent`, which the service shares between all of its clients |
| `max_response_bytes` | At least 262144. Tool payloads are bounded at 240 KiB and the default of 1 MiB leaves room for the envelope. |

### Start and troubleshoot

Start “QCopilots MCP Server Web Search” in the Manager. Its default local MCP URL
is `http://127.0.0.1:48218/mcp`. External-browser testing also requires
`QCOPILOTS_EXTERNAL_BROWSER_ACCESS=ON`, the configured WebUI Origin, and the
Manager's local Bearer credential. Do not paste the native backend token into
the local MCP entry.

The adapter remains discoverable when its backend is offline. Initialization,
tool listing and local health do not depend on a successful public search. Calls
return explicit backend errors and recover when the backend becomes available.

| Adapter error | Usual cause |
| --- | --- |
| `forbidden` | The `backend_url` authority is missing from `business.allowed_hosts`, or the filesystem policy denies the adapter configuration |
| `unauthorized` | The two token values differ, or the service was not restarted after adding the client |
| `backend_unavailable` | The service is not running, listens on another port or returned an unexpected response |
| `invalid_configuration` | The adapter configuration or its token source is invalid |
| `timeout` | `timeout_seconds` is shorter than the service deadline, or the upstream is slow |
