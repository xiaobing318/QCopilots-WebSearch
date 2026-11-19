# Team deployment

The reference deployment targets 10–50 team members, with five concurrent
business operations by default. This is a capacity setting, not a promise of
five simultaneous upstream successes. Public engines can return partial results,
CAPTCHAs or throttling. The service never bypasses those restrictions.

## Ubuntu host

Install the release DEB using apt. The package creates the unprivileged
`qcopilots-websearch` account, separate random client and admin credentials, and a systemd unit.

```bash
sudo apt install ./qcopilots-websearch_<version>_amd64.deb
sudo systemctl status qcopilots-websearch
```

The deployment configuration is `/etc/qcopilots-websearch/runtime.json`.
The service owns `client.token` and `admin.token` with mode 0600. It stores
user settings and provider keys under `/var/lib/qcopilots-websearch`.
The administrator can edit the deployment configuration, and the service can
read it.

Create a one-time management link with

```bash
sudo /opt/qcopilots-websearch/bin/qcopilots-websearch --ConfigPath /etc/qcopilots-websearch/runtime.json --SettingsUrl
```

Open the link in a browser on the service host. For a remote browser, use an
SSH tunnel with the matching loopback address, or configure a dedicated HTTPS
proxy and set the admin Host, Origin and public origin accordingly. The
reference Caddy deployment publishes the business service.

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

In Settings, select SearXNG and set its endpoint to `http://127.0.0.1:8888/search`. Add the real external
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
Origins and hosts. Keep 8088, 8089, 8090, 8091 and 8888 inaccessible from external hosts.

The SearXNG limiter is disabled in this private backend template. SearXNG is not
an independently public endpoint. Exposing it requires a separate authentication,
rate-control and proxy-address design. Fixed container image digests are committed
in `compose.yml`. Upgrade them deliberately and repeat acceptance.

## Windows

The MSI installs the `QCopilotsWebSearch` machine service under the virtual
account `NT SERVICE\QCopilotsWebSearch`. Program files and Qt libraries go under
Program Files. Deployment configuration and credentials live in
`%ProgramData%\QCopilots-WebSearch`. The service loads `service.json` from that
directory. User settings and provider keys live in its `state` subdirectory.
Windows uses Schannel.

Local interactive users can read `admin.token` to open the settings page and
manage search settings. Runtime configuration, the client token and provider
secret files retain their own ACLs. From the installed `bin` directory, run
`qcopilots-websearch.exe --SettingsUrl` to create a one-time link for the
default MSI installation.

The MSI needs elevation. Read the Apache and separate Microsoft runtime terms
shown by the installer. The package is unsigned unless stated in its release notes. Uninstall removes program files and the service while retaining
generated configuration and data. Do not delete retained data unless intentionally
resetting the deployment.

Extract the portable ZIP to a writable directory, including one with spaces or
Unicode, and use `bin/qcopilots-websearch.exe` to initialize and run the service.
Issue a link with `--ConfigPath FILE --SettingsUrl`. The ZIP runs as a foreground
process. New configurations use the automatic source, so a SearXNG instance is optional.

## Manage client tokens

The CLI can generate a token or register one you already have for an existing
runtime configuration. Both commands ask for confirmation on stderr and require
`y` or `yes`. The setter reads the token from standard input and masks it in an
interactive terminal. Enter the token as one line of 32 to 8192 visible ASCII
characters, then confirm on the next line. The generator prints the new token
to stdout after the configuration is saved.

```text
qcopilots-websearch --ConfigPath FILE --GenerateToken [--ClientId ID]
qcopilots-websearch --ConfigPath FILE --SetToken [--ClientId ID]
```

The default client ID is `local`. Specify a different ID to grant an individual
user or integration its own token. The command adds a new ID or replaces the
credential for an existing one while preserving that client's cache identity.
The updated entry stores only `token_sha256`. Existing entries can continue to
use `token_env` or `token_file`. The service reads credentials at startup, so
restart it to apply changes and invalidate any replaced token.

Keep the token value in a private credential store and share it securely with
the client. The digest in the configuration cannot be used to connect or recover
the token. The standalone `--GenerateToken` command prints a token without
changing any configuration.

For the DEB installation, generate a credential as an administrator, check the
configuration as the service account, then restart.

```bash
sudo /opt/qcopilots-websearch/bin/qcopilots-websearch --ConfigPath /etc/qcopilots-websearch/runtime.json --GenerateToken --ClientId team-alice
sudo -u qcopilots-websearch /opt/qcopilots-websearch/bin/qcopilots-websearch --ConfigPath /etc/qcopilots-websearch/runtime.json --CheckConfig
sudo systemctl restart qcopilots-websearch
```

For the MSI installation, run these commands from the installed `bin` directory
in an elevated PowerShell session. For a portable installation, use its runtime
configuration path and omit `--ServiceName`.

```powershell
$configPath = Join-Path $env:ProgramData 'QCopilots-WebSearch\service.json'
.\qcopilots-websearch.exe --ConfigPath $configPath --ServiceName QCopilotsWebSearch --GenerateToken --ClientId team-alice
.\qcopilots-websearch.exe --ConfigPath $configPath --ServiceName QCopilotsWebSearch --CheckConfig
Restart-Service -Name QCopilotsWebSearch
```

## Connect remote clients

Use the existing HTTPS endpoint for the business listener. Include its exact
authority in `business.allowed_hosts` and the exact browser page origin in
`business.origins` when connecting directly from a browser. The endpoint
forwards the client's Bearer credential to QCopilots-WebSearch.

In the client's MCP server settings, enter `https://search.example.org/mcp`
using your service hostname. Enable **Authorization** and paste the token value
into the **Bearer** field. Verify the client lists the four tools and can call
`web_search_status`. For the [QGIS adapter](#qgis), use the HTTPS origin
`https://search.example.org` as `backend_url`, with its existing token source.

## Runtime trust and proxies

Runtime HTTPS uses the trust sources described in [architecture](architecture.md).
Install the complete server certificate chain at the HTTPS endpoint. Windows may retrieve an intermediate
issuer through AIA. Linux relies on the supplied chain and its local trust store.
Both platforms omit online CRL and OCSP checks. Runtime requests
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

1. Generate and register a token with `qcopilots-websearch --ConfigPath FILE
   --GenerateToken --ClientId qgis-alice`. Use the
   [platform-specific steps](#manage-client-tokens) to check the configuration
   and restart the service.
2. Give the user the token value in a file only that user can read, or in the
   user environment variable `QCOPILOTS_WEBSEARCH_BACKEND_TOKEN`. On Linux and
   macOS the adapter rejects a token file that other users can read.

A single-user setup created with `--InitializeConfig` already has a generated
`client.token`. If the service still uses it and QGIS can read it locally,
point the adapter's `auth.token_file` at that absolute path.

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

The separate `settings` object can set `url` to the admin origin and supply an
`admin_token_file` or `admin_token_env`. It uses the management credential
independently of the backend business credential. The Web Search card's menu
opens a one-time login link when a management credential is available, or
opens the manual login page otherwise.

### Start and troubleshoot

Start “QCopilots MCP Server Web Search” in the Manager. Its default local MCP URL
is `http://127.0.0.1:48218/mcp`. External-browser testing also requires
`QCOPILOTS_EXTERNAL_BROWSER_ACCESS=ON`, the configured WebUI Origin, and the
Manager's local Bearer credential. Do not paste the native backend token into
the local MCP entry.

With “Search without asking” off, QGIS asks before the first search or open of a
new URL in its current session. After a refusal, it waits 120 seconds before
prompting again. Cached page reads, find and status need no confirmation.

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
