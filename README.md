# QCopilots-WebSearch

[简体中文](README.zh-CN.md)

QCopilots-WebSearch is an Apache-2.0 C++20 and Qt 6.12 service for web search, page reading and literal text search. It ships a settings page in English and Simplified Chinese and supports Windows 11 and Ubuntu 26.04 on amd64.

Search works without an API key through DuckDuckGo HTML, DuckDuckGo Lite and Bing fallback. In Settings, choose a private SearXNG instance or supply an API key for Brave, Tavily, Exa, Perplexity or DeepSeek. Availability and rate limits depend on the selected source.

## Install and open Settings

| Platform | Package | Settings command |
| --- | --- | --- |
| Windows 11 | MSI. Registers a service for the installed version | Run `qcopilots-websearch.exe --SettingsUrl` from that version's installed `bin` directory |
| Windows 11 | ZIP. Extract and initialize in a private writable directory | Run `bin/qcopilots-websearch.exe --ConfigPath <file> --SettingsUrl` |
| Ubuntu 26.04 | DEB. Install `qcopilots-websearch-<version>_<version>_amd64.deb` with apt | Run `sudo /opt/qcopilots-websearch-<version>/bin/qcopilots-websearch --SettingsUrl` |

Different MSI or DEB versions can run on the same host. Each version starts with
its own configuration, random client and admin tokens, settings and cache. The
installer selects free loopback ports on first install. Use the ports and
credentials recorded for the version you want to access.

For a portable installation, initialize the configuration and start the service first.

```text
qcopilots-websearch --InitializeConfig --ConfigPath /private/path/service.json
qcopilots-websearch --ConfigPath /private/path/service.json
```

The Settings command prints a one-time link. Open it within two minutes, or append `--Open` to launch the browser. You can also open `/settings/` on the instance's admin listener and sign in with its `admin.token`.

Use the business listener's `/v1` REST routes or `/mcp` with a client Bearer token. Health and metrics use the control listener. Process-based MCP clients can use `--Stdio --ConfigPath FILE`. [Deployment and QGIS setup](docs/deployment.md)

Remote MCP clients connect over HTTPS at `https://<public-host>/mcp` with a Bearer token. Configure the token with `--ConfigPath FILE --GenerateToken` or `--ConfigPath FILE --SetToken`. [Client token management](docs/deployment.md#manage-client-tokens) · [Remote client configuration](docs/deployment.md#connect-remote-clients)

## MCP tools

| Tool | Behavior |
| --- | --- |
| `web_search` | Searches the selected source and applies strict domain filters |
| `web_open` | Reads public HTML or text into a client-scoped page with numbered lines |
| `web_find` | Finds literal text in a stored page |
| `web_search_status` | Reads the current tool settings and last known provider state |

Use Settings to enable search and page opening independently. When “Search without asking” is off, the QGIS adapter asks once per QGIS session before a search or new URL open. Direct REST and MCP clients manage their own confirmation. The [versioned API](docs/api.md) defines cache behavior, provenance and errors.

## Build and test

Windows requires Visual Studio 2022 or 2026 with C++ and a Windows SDK, CMake 3.30 or newer, Ninja, Git and Python 3.12 or newer. Ubuntu requires Python 3.12 or newer before bootstrap. Bootstrap prepares Qt, Node and the required build dependencies.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/BuildPipeline.ps1 --Profile windows-x64 --Stages bootstrap,doctor,configure,build,test,install
```

```bash
bash scripts/BuildPipeline.sh --Profile ubuntu-x64 --Stages bootstrap,doctor,configure,build,test,install
```

Use `--ConfigPath` with a private `*.local.json` derived from `config/BuildPipeline.json`. Add `publish` to create local packages. Windows MSI packaging requires WiX 7.0.1 and .NET 10. GitHub Actions builds on Windows Server 2025 and Ubuntu 26.04. A version tag runs release acceptance and publishes the tested packages with checksums, matching Qt source and provenance. [Build instructions](docs/build.md) · [Release workflow](docs/release.md)

## Repository layout

- `app`, `include/qws`, `src` contain the native service and CLI.
- `web` contains the React and TypeScript frontend embedded as Qt resources.
- `contracts/v1` and `contracts/admin/v1` define the MCP and management interfaces.
- `config`, `cmake`, `scripts`, `packaging` and `deploy` cover the build and installation.
- `tests` contains deterministic native and Python fixtures and fuzz targets.

See [runtime architecture](docs/architecture.md), [operations](docs/operations.md) and [licensing](docs/licensing.md).
