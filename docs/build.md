# Build and package

The service supports Windows 11 and Ubuntu 26.04 on amd64. Automated Windows builds run on Windows Server 2025. A local Windows 11 run checks the portable ZIP.

## Prerequisites

- Windows needs Visual Studio 2022 or 2026 with Desktop development with C++, a Windows SDK, CMake 3.30 or newer, Ninja, Git and Python 3.12 or newer. The pipeline discovers the x64 tools and can use CMake and Ninja from Visual Studio. TLS uses Schannel.
- Ubuntu needs Python 3.12 or newer with venv and pip before running the Bash entry point. Bootstrap installs GCC, CMake, Ninja, Git, distribution OpenSSL development files and other native requirements with apt. System package installation requires root or noninteractive sudo.
- Bootstrap prepares Qt 6.12.0, Node 24.22.3 and isolated Python tooling. Node is a build-time dependency of the embedded settings UI. The native installed service includes the UI and uses shared Qt libraries.

## Build and configure

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/BuildPipeline.ps1 --ConfigPath config/BuildPipeline.json --Profile windows-x64 --Stages bootstrap,doctor,configure,build,test,install,publish
```

```bash
bash scripts/BuildPipeline.sh --ConfigPath config/BuildPipeline.json --Profile ubuntu-x64 --Stages bootstrap,doctor,configure,build,test,install,publish
```

Both wrappers call the same Python implementation. The requested stages run in order. `--CheckOnly` reports prerequisites without installing and `--DryRun` prints the proposed commands.

Use a private copy of the configuration ending in `.local.json` to change local build, install, cache or tool paths. Relative paths resolve from the repository and environment variables in profile values are expanded. The default Windows Qt root is `C:/Softwares/Qt`. Select only ZIP by setting a Windows profile's `packageGenerators` to `["ZIP"]`.

The configure step uses CMake Presets `windows-x64`, `ubuntu-x64` and `ubuntu-x64-quality`. Native CI profiles set `QCWS_BUILD_WEB=OFF` and point `QCWS_WEB_DIST_DIR` to the prebuilt bundle from the web job. Local configuration builds the frontend by default.

Tool and source downloads are verified against committed hashes. Set `proxy` in a private profile to control downloads. An empty value selects direct access. The profile proxy also applies to apt, pip and npm. Runtime outbound requests independently use direct connections. Use `offline: true` with preinstalled system tools and verified caches. Building the frontend offline requires an npm cache populated with the locked packages, or a prebuilt bundle.

## Windows MSI tooling

With a .NET 10 SDK available, install official WiX 7.0.1 and its matching UI extension.

```text
dotnet tool install --tool-path <tool-directory> wix --version 7.0.1
wix extension add -g WixToolset.UI.wixext/7.0.1 -acceptEula wix7
```

Add the tool directory to PATH or set `tools.wix` in the private profile. CPack uses its WiX .NET driver with `CPACK_WIX_VERSION=4`. See [licensing](licensing.md) for the WiX terms.

The `publish` stage produces local packages in the profile's `buildDir/packages/<timestamp>`. MSI installation needs elevation to register the machine service. The [GitHub release workflow](release.md) publishes packages after building and testing a version tag.

## Test

CTest uses local deterministic fixtures for TLS, HTTP, settings, search providers and MCP. To check the frontend separately, use the Node pin in `.nvmrc`.

```bash
cd web
npm ci --ignore-scripts
npm run lint
npm run format:check
npm run typecheck
npm run test:ci
npm run build
```

For installed binaries, install `tests/requirements.lock` in an isolated Python environment with `pip --require-hashes`, then run the SDK check.

```text
python scripts/test_mcp_sdk.py --Executable <installed-binary> --Output <report.json>
```

Add `--LiveProvider auto` to exercise actual public search sources. Keep those results separate from the deterministic suite.

CI tests the Windows ZIP and Ubuntu DEB against their installed executables. Release and manual CI use two baselines to test MSI and DEB co-installation, per-version configuration and credentials, and removal of one version while the others remain available. Version 0.0.1 uses the current package layout. Version 0.2.0 is built from the revision that stored data in system directories. Use an elevated disposable Windows environment for `scripts/AcceptWindowsMsi.ps1`. These runs also verify the Caddy deployment.
