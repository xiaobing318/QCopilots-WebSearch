# Build and package

Supported runtime targets are Windows 11 amd64 and Ubuntu 26.04 amd64. Windows
Server 2022 CI is build evidence, not a substitute for Windows 11 acceptance.
Use C++20, Qt 6.12.0 shared libraries, CMake and Ninja.

## One entry point on each platform

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/BuildPipeline.ps1 --ConfigPath config/BuildPipeline.json --Profile windows-x64 --Stages bootstrap,doctor,configure,build,test,install,publish
```

```bash
bash scripts/BuildPipeline.sh --ConfigPath config/BuildPipeline.json --Profile ubuntu-x64 --Stages bootstrap,doctor,configure,build,test,install,publish
```

Both wrappers call the same Python implementation. Parameters use the same
spelling and case. Stages execute in the supplied order and must be unique.
`doctor,bootstrap,configure,build,test,install,publish` are the supported stages.
There is no implicit clean step.

`--CheckOnly` runs diagnostics without installation. `--DryRun` prints planned
commands without downloading or changing the toolchain. Full schema validation
requires the pinned Python tooling installed by bootstrap.

Windows needs VS2022 with Desktop development with C++, a Windows SDK, CMake
3.30 or newer for MSI, Ninja, Git and Python 3.12 or newer. The script discovers
VS2022 and imports its x64 environment, including its bundled CMake/Ninja.
Windows bootstrap downloads Qt and isolated Python tooling. It does not install
Visual Studio or a Windows compiler. Missing prerequisites produce diagnostics.

The Windows OpenSSL build also requires native Windows Perl, such as Strawberry
Perl 5.42.3.1. Set `tools.perl` or put its `perl.exe` on PATH. Git/MSYS/Cygwin Perl
is rejected because its path model does not match the MSVC build. Bootstrap does
not install this toolchain prerequisite. CI prepares pinned official packages
using `scripts/ci_windows_tools.py --IncludePerl` in a private tools directory.

Ubuntu bootstrap uses apt to install GCC, CMake, Ninja, Git, Python and runtime
development dependencies, then installs the same fixed Qt version from its
verified archive. Root or noninteractive sudo is required for missing system
packages. Linux does not use MSVC. Qt's vendor platform table is distinct from
the Ubuntu 26.04 build/runtime evidence produced by this project.

On a minimal Ubuntu image without Python, the Bash entry point uses the bundled
JSON::PP parser and the Essential `perl-base` package to read the selected profile.
Only an online `bootstrap` stage installs Python. `--CheckOnly` reports the
missing interpreter and `--DryRun` prints the installation plan without running
APT. An offline bootstrap requires Python and other system packages to be present.
The APT calls explicitly apply the profile proxy through sudo. An empty proxy
selects direct access, including when the host has other proxy settings.
Pinned archive downloads and pip also use the profile's proxy. Inherited
`ALL_PROXY` and bypass lists do not override it. An empty profile proxy forces
direct access. Pip machine/user configuration files are disabled for isolated
tool bootstrap, so their proxy settings cannot silently change this decision.

## Configuration and downloads

Copy `config/BuildPipeline.json` to a file ending in `.local.json`. That suffix
is ignored by Git. Select it with `--ConfigPath`. Paths for build, installation,
download cache and Qt can be changed independently. Relative paths resolve from
the source repository. The Windows Qt default is `C:/Softwares/Qt`.

`tools` allows explicit executable paths and a VS2022 installation root. Empty
values use discovery. `proxy` accepts an HTTP(S) proxy such as
`http://127.0.0.1:7897`. It affects downloads, not runtime public-page fetching.
Do not put credentials in committed proxy URLs.

Archives and Python packages use committed versions and hashes. Downloads are
retried and verified before extraction. Corrupt cached files are not trusted.
Qt and source caches can be reused across builds. Complete an online bootstrap
and configure, copy the cache and matching Qt installation, then set `offline`
to true to reuse them without network downloads. Missing system packages or
verified cache entries fail explicitly. Python wheel caches are platform- and
Python-minor-version-specific.

The schema documents each field's purpose, allowed values and effect. Unknown
fields fail validation. Do not reuse a source root as an install/cache directory.

## Windows MSI tooling

Use official WiX 7.0.1, WixToolset.UI.wixext 7.0.1 and .NET 10 LTS. Install tools
into a dedicated directory and add it to PATH, or specify executable paths in
the private build configuration. For example, with a .NET 10 SDK available:

```text
dotnet tool install --tool-path <tool-directory> wix --version 7.0.1
wix extension add -g WixToolset.UI.wixext/7.0.1 -acceptEula wix7
```

CPack's `CPACK_WIX_VERSION=4` selects its modern .NET WiX driver. It does not mean
the installed WiX tool is version 4. The build checks for 7.0.1 and passes the
official WiX 7 EULA acknowledgment required by that tool. Review the fee terms
described in [licensing](licensing.md) before use.

`publish` builds local ZIP/MSI or DEB files. It never uploads a GitHub Release.
Each completed configure/build/test/install stage writes a receipt binding the
source bytes and selected profile. Tests also bind their executables and runtime
libraries. A source or profile change requires repeating these stages before
packaging. Failed stages invalidate prior receipts. ZIP and MSI payloads must
match, and each package receives its own file inventory and SBOM.
MSI installation requires elevation because it registers a machine service.
Packages are unsigned unless a release explicitly supplies signing evidence.

## Tests

The core CTest suite uses temporary storage, dynamic ports, local HTTP/TLS
fixtures and injected clocks. It does not require public search engines.

For additional installed-binary acceptance, create an isolated Python environment
and install `tests/requirements.lock` with `pip --require-hashes`. Then run:

```text
python scripts/test_mcp_sdk.py --Executable <installed-binary> --Output <report.json>
python scripts/soak.py --Executable <installed-binary> --Output <report.json>
```

The soak default is 300 seconds (five minutes) per phase, cold then hot. `--Seconds 10` is a
smoke test and cannot satisfy the full stability gate. It removes its temporary
state and stops only child processes that it created.

The low-version installer baseline is a test artifact built from the same source
with `-DQCWS_VERSION=0.0.1`. It is not an earlier supported public release.
