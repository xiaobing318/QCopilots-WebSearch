# Team deployment

The reference deployment targets 10–50 team members, with five concurrent
business operations by default. This is a capacity setting, not a promise of
five simultaneous upstream successes. Public engines can return partial results,
CAPTCHAs or throttling. The service never bypasses those restrictions.

## Installed versions and ports

MSI and DEB packages install each project version as a separate service. A new
version starts with fresh deployment configuration, client and admin credentials,
settings, provider keys and cache. Each version's `bin/data` directory holds
the runtime file and credentials, with settings, provider keys and cache under
`bin/data/state`. Reinstalling into the same installation directory reuses
that version's data. Earlier versions keep their existing directories and
configuration.

The installer tries the default business, control and admin loopback ports
(8088, 8089 and 8091). It skips ports in use or already recorded by another
installed version, including when that version is stopped.
It records the chosen ports in the version's runtime configuration. Use its
`business.port` for REST and MCP, `control.port` for health and metrics, and
`admin.port` for Settings. Use that same version's tokens when connecting.
Restarts reuse the recorded ports.

## Ubuntu host

Install the release DEB using apt. Set `version` to the version in the package
filename. Each package creates a version-specific unprivileged service account
and systemd unit.

```bash
version=0.2.1
package=qcopilots-websearch-$version
sudo apt install "./${package}_${version}_amd64.deb"
sudo systemctl status "$package.service"
```

The systemd service loads
`/opt/qcopilots-websearch-<version>/bin/data/runtime.json`. The administrator
can edit it, and the corresponding service account can read it. That account
owns the adjacent `client.token` and `admin.token` with mode 0600.

Run the executable for the selected version to create its one-time management
link.

```bash
sudo "/opt/$package/bin/qcopilots-websearch" --SettingsUrl
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

In the copied `Caddyfile`, set the reverse proxy upstream to
`127.0.0.1:<business.port>` from the runtime configuration of the version to
publish.

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
Validate the selected version's configuration as its service account, then
restart that version.

```bash
account=$(systemctl show --property=User --value "$package.service")
sudo -u "$account" "/opt/$package/bin/qcopilots-websearch" --CheckConfig
sudo systemctl restart "$package.service"
```

Caddy preserves client Authorization, Host and Origin. It does not substitute a
shared gateway token. Do not add `flush_interval -1`, because it changes upstream
cancellation behavior after client disconnect. The native service rejects unknown
Origins and hosts. Keep the native listeners, the Caddy health listener and
SearXNG inaccessible directly from external hosts.

The SearXNG limiter is disabled in this private backend template. SearXNG is not
an independently public endpoint. Exposing it requires a separate authentication,
rate-control and proxy-address design. Fixed container image digests are committed
in `compose.yml`. Upgrade them deliberately and repeat acceptance.

## Windows

The MSI installs the `QCopilotsWebSearch-<version>` machine service under the
virtual account `NT SERVICE\QCopilotsWebSearch-<version>`. Its default program
directory is `%ProgramFiles%\QCopilots-WebSearch-<version>`. The service loads
`bin\data\runtime.json` within that directory.

To use a different directory, set `INSTALL_ROOT` from an elevated Command
Prompt. The program and its `bin\data` directory both use that location.
Choose a separate root for each version.

```cmd
msiexec.exe /i "C:\Downloads\QCopilots-WebSearch-0.2.1-win64.msi" INSTALL_ROOT="C:\Program Files\QCopilots-WebSearch-Custom-0.2.1"
```

Windows uses Schannel.

Local interactive users can read `admin.token` to open the settings page and
manage search settings. Runtime configuration, the client token and provider
secret files retain their own ACLs. Run `qcopilots-websearch.exe --SettingsUrl`
from the installed `bin` directory of the desired version to create a one-time
link for its running service.

The MSI needs elevation. Read the Apache and separate Microsoft runtime terms
shown by the installer. The package is unsigned unless stated in its release
notes. Use the version shown in Installed apps to select an MSI to uninstall.
Uninstall removes that version's program files and service and retains its
configuration and data.

Extract the portable ZIP to a private writable directory. Use a separate
extraction directory for each portable instance. From its root, run
`.\bin\qcopilots-websearch.exe` to start in the foreground. The first launch
creates `bin\data\runtime.json` and generates client and admin credentials.
Run `.\bin\qcopilots-websearch.exe --SettingsUrl` from another terminal to
open Settings for that instance.

Use `--ConfigPath FILE` to select another file. To initialize a custom path,
pass `--InitializeConfig --ConfigPath FILE`. Add `--SelectAvailablePorts` to
choose free loopback ports for the new configuration. New configurations use
the automatic search source, so a private SearXNG instance is optional.

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
configuration as that version's service account, then restart.

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

For the MSI installation, set `installRoot` to the directory used by the
installer. Run these commands from an elevated PowerShell session. For a
portable installation, use its runtime configuration path and omit
`--ServiceName`.

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

## Connect remote clients

Use the HTTPS endpoint for the selected version's business listener. Include its exact
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
