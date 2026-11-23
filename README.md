# QCopilots-WebSearch

[简体中文](README.zh-CN.md)

QCopilots-WebSearch is an Apache-2.0 C++20 and Qt 6.12 service for web search, page reading and literal text search. It ships a settings page in English and Simplified Chinese and supports Windows 11 and Ubuntu 26.04 on amd64.

Search works without an API key through DuckDuckGo HTML, DuckDuckGo Lite and Bing fallback. In **Search settings**, choose a private SearXNG instance or supply an API key for Brave, Tavily, Exa, Perplexity or DeepSeek. Availability and rate limits depend on the selected source.

## Install and open Settings

| Platform | Package | Settings command |
| --- | --- | --- |
| Windows 11 | MSI. Registers a service for the installed version | Run `qcopilots-websearch.exe --SettingsUrl` from that version's installed `bin` directory |
| Windows 11 | ZIP. Extract to a private writable directory and start the executable | Run `.\bin\qcopilots-websearch.exe --SettingsUrl` |
| Ubuntu 26.04 | DEB. Install `qcopilots-websearch-<version>_<version>_amd64.deb` with apt | Run `sudo /opt/qcopilots-websearch-<version>/bin/qcopilots-websearch --SettingsUrl` |

Different MSI or DEB versions can run on the same host. Each version keeps
its configuration, credentials, settings and cache in its own `bin/data`
directory. The installer selects free loopback ports on first install. Use the
ports and credentials recorded for the version you want to access.

To start the portable Windows service, run this from the extracted directory.

```powershell
.\bin\qcopilots-websearch.exe
```

The first launch initializes `bin/data/runtime.json` and prints the actual
business, control and Settings addresses, the configuration path and how to
sign in. New portable instances use fixed ports 3181, 3182 and 3183 respectively
and fail to start if any is occupied. Existing instances keep their recorded
ports. Run the Settings command from a separate
terminal while the portable service runs.

The Settings command prints a one-time link. Open it within two minutes, or append `--Open` to launch the browser. You can also open `/settings/` on the instance's admin listener and sign in with its `admin.token`.

Use the business listener's `/v1` REST routes or `/mcp` with a client Bearer token. Health and metrics use the control listener. Process-based MCP clients can use `--Stdio --ConfigPath FILE`. [Deployment and QGIS setup](docs/deployment.md)

Any HTTP(S) web application can call the business API with a valid Bearer token. There is no per-site `business.origins` list. Native and server-side clients can omit `Origin`. New runtime configurations allow `127.0.0.1:3181` and `web-search.vastarmorai.com` as business Hosts and set `business.public_url` to `https://web-search.vastarmorai.com/mcp`. If initialization selects another port, the loopback Host uses that port. The public URL is connection information only and does not create a tunnel or verify reachability. For another deployment, set its actual Hosts and public URL. Existing configurations that contain `business.origins` must have that field removed before they can pass `--CheckConfig`. Control and Settings access remain separately restricted. [Business configuration](docs/deployment.md#business-addresses-and-browser-access)

The management page has **Access authorization**, **Search settings** and **Service status** tabs. In Access authorization, the client list is on the left and the selected client's details are on the right. Add a client or replace its code in three steps: fill in or generate a code, review and choose **Save and activate**, then copy the active Bearer and server URL. Saving takes effect immediately and survives restart. QCopilots uses the Bearer with its existing registered server URL.

You can enter a code containing `/`, generate a random token, or choose **Generate from text**. Text generation produces 64 lowercase hexadecimal characters from SHA-256 of the exact UTF-8 text without trimming spaces or changing case. The source text stays in the page and is cleared after generation. After saving, you can copy the code repeatedly while the connection-details step remains open. The code is held only in page memory and cleared when you finish, switch client or tab, refresh, or sign out. It cannot then be retrieved from the saved client list. [Client token management](docs/deployment.md#manage-client-tokens)

When Cloudflare Tunnel and WebSearch run on the same Windows host, point the tunnel to the local business listener, normally `http://127.0.0.1:3181`. On QCopilots' MCP Servers page, keep the Web Search server's registered URL, enable Authorization and enter the client Bearer token. The plugin relays MCP to `https://web-search.vastarmorai.com/mcp` without a separate backend configuration file or authorization prompt. Other MCP clients connect directly to that public `/mcp` endpoint. [Windows Tunnel quick setup](docs/deployment.md#windows-cloudflare-tunnel) · [QGIS setup](docs/deployment.md#qgis)

## MCP tools

| Tool | Behavior |
| --- | --- |
| `web_search` | Searches the selected source and applies strict domain filters |
| `web_open` | Reads public HTML or text into a client-scoped page with numbered lines |
| `web_find` | Finds literal text in a stored page |
| `web_search_status` | Reads the current tool settings and last known provider state |

Use **Search settings** to enable search and page opening independently. QGIS relays tool calls without adding a search confirmation step. The compatibility setting `search_without_asking` does not change that behavior. **Service status** shows the service version, listening addresses and provider availability. Service health describes process readiness separately from search-provider availability. The [versioned API](docs/api.md) defines cache behavior, provenance and errors.

## Build and test

Windows requires Visual Studio 2022 or 2026 with C++ and a Windows SDK, CMake 3.30 or newer, Ninja, Git and Python 3.12 or newer. Ubuntu requires Python 3.12 or newer before bootstrap. Bootstrap prepares Qt, Node and the required build dependencies.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/BuildPipeline.ps1 --Profile windows-x64 --Stages bootstrap,doctor,configure,build,test,install
```

```bash
bash scripts/BuildPipeline.sh --Profile ubuntu-x64 --Stages bootstrap,doctor,configure,build,test,install
```

Use `--ConfigPath` with a private `*.local.json` derived from `config/BuildPipeline.json`. Add `publish` to create local packages. Windows MSI packaging requires WiX 7.0.1 and .NET 10. GitHub Actions builds on Windows Server 2025 and Ubuntu 26.04. Test the feature branch's Actions package before merging. A version tag can publish only after the same commit belongs to main and its main push CI succeeds. Release acceptance then publishes tested packages with checksums, matching Qt source and provenance. [Build instructions](docs/build.md) · [Release workflow](docs/release.md)

## Repository layout

- `app`, `include/qws`, `src` contain the native service and CLI.
- `web` contains the React and TypeScript frontend embedded as Qt resources.
- `contracts/v1` and `contracts/admin/v1` define the MCP and management interfaces.
- `config`, `cmake`, `scripts`, `packaging` and `deploy` cover the build and installation.
- `tests` contains deterministic native and Python fixtures and fuzz targets.

See [runtime architecture](docs/architecture.md), [operations](docs/operations.md) and [licensing](docs/licensing.md).
