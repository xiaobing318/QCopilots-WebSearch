# QCopilots-WebSearch

[简体中文](README.zh-CN.md)

An Apache-2.0 C++20 / Qt 6.12 service for self-hosted web search, HTML reading and literal page search. SearXNG supplies search results. REST, MCP HTTP and MCP stdio share the same versioned tools. A separate GPL adapter connects QGIS QCopilots to the service.

Windows and Ubuntu compilation, packaging and automated validation are implemented. Ubuntu acceptance has exercised the SDK, DEB lifecycle, Caddy HTTPS, 60 public queries, sanitizers, static analysis and bounded fuzzing. Final delivery still requires complete candidate-specific stability evidence, Windows 11 production MSI acceptance and the independent review council. A version tag creates candidates. Only a verified, promoted Release represents an accepted distribution. See [release gates](docs/release.md).

## Tools

| Tool | Purpose |
| --- | --- |
| `web_search` | Search through a configured SearXNG instance, with strict domain filtering |
| `web_open` | Read public HTML/text into an identity-scoped snapshot with numbered lines |
| `web_find` | Find literal text in that snapshot, with Unicode case-insensitive matching |
| `web_search_status` | Read local and last-known dependency state without searching the web |

No paid provider, independent web index, PDF extraction, JavaScript execution or LLM answer generation is included. Self-hosting removes a software-imposed commercial quota, but upstream search engines can still throttle or block requests.

## Build

Windows requires VS2022 with the C++ workload and Windows SDK, CMake 3.30+, Ninja, Git, native Windows Perl and Python 3.12+. Bootstrap installs the fixed Qt archives and isolated Python download tooling. It does not install Visual Studio or Perl. Ubuntu bootstrap installs its compiler and system dependencies using apt.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/BuildPipeline.ps1 --Profile windows-x64 --Stages bootstrap,doctor,configure,build,test,install
```

```bash
bash scripts/BuildPipeline.sh --Profile ubuntu-x64 --Stages bootstrap,doctor,configure,build,test,install
```

Use `--ConfigPath` to select a private configuration copied from `config/BuildPipeline.json`. Files ending in `.local.json` are ignored. `--CheckOnly` diagnoses without installing, and `--DryRun` prints planned commands. A download proxy can be configured as `http://127.0.0.1:7897`. This setting does not proxy runtime webpage requests.

`--Stages publish` generates local ZIP/MSI or DEB packages. It does not publish a GitHub Release. MSI requires official WiX 7.0.1, its matching UI extension and .NET 10. See the official package's maintenance-fee terms before choosing that distribution.

## Run

Initialize a configuration in a private writable directory. The command creates a random client token file and preserves existing configuration.

```text
qcopilots-websearch --InitializeConfig --ConfigPath /private/path/service.json
qcopilots-websearch --CheckConfig --ConfigPath /private/path/service.json
qcopilots-websearch --ConfigPath /private/path/service.json
```

Set `provider.url` to the private SearXNG `/search` endpoint with JSON enabled. The default business listener is `127.0.0.1:8088`, and authenticated health/metrics use `127.0.0.1:8089`. Configure independent credentials per client. Remote access requires an HTTPS reverse proxy with exact Host and Origin settings.

REST uses `POST /v1/search`, `/v1/open`, `/v1/find` and `GET /v1/status`, with `Authorization: Bearer ...`. MCP uses `/mcp` and protocol `2025-06-18`. For a process-based client, use `--Stdio --ConfigPath FILE`. Stdio has private temporary state and does not open HTTP listeners.

Web content is untrusted source material. Tools return provenance and bounded text, not instructions that clients should execute. Public fetching blocks non-public addresses, checks the actual connection destination, disables inherited proxies and checks every redirect.

## Project layout

- `app`, `include/qws`, `src`: native application and reusable service components
- `contracts/v1`: canonical tool schemas
- `config`: build/runtime examples, schemas and fixed dependency hashes
- `scripts`, `cmake`: shared build and distribution tooling
- `tests`: deterministic tests with temporary state and dynamically allocated ports
- `docs`: architecture, acceptance and review records

See [the design and acceptance contract](docs/design.md). Native source uses Apache-2.0. Third-party libraries retain their own licenses. Qt is dynamically linked and release artifacts must include its matching source and replacement instructions. QGIS adaptation code remains in the QGIS GPL repository.

Read the [research](docs/research.md), [runtime architecture](docs/architecture.md),
[build instructions](docs/build.md), [team deployment](docs/deployment.md),
[operations guide](docs/operations.md), [release gates](docs/release.md) and [license details](docs/licensing.md).
