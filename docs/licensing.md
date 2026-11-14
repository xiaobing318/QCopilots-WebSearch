# Licensing and replaceable libraries

Project-owned native source is Apache-2.0. The separate QGIS plugin is
GPL-2.0-or-later and is maintained in the QGIS repository. Neither license
relicenses the other project's source or third-party libraries.

## Qt

We dynamically link the Qt 6.12.0 Core, Network and SQL libraries. The SQLite,
network-information and TLS plugins retain their upstream licenses. Qt HTTP
Server is not used. Our Qt use follows the LGPL-3.0 option, including the
applicable GPL-3.0 terms incorporated by that license.

Each release includes the unmodified matching `qtbase-everywhere-src-6.12.0`
archive, its SHA-256, upstream license texts, Qt build options and original Qt
SBOM. The vendor SBOM describes the larger qtbase distribution. Our package SBOM
separately inventories the files actually shipped.

The installed program does not authenticate Qt DLLs or require a vendor signature.
Users may rebuild Qt and replace its shared libraries and corresponding plugins.
Reverse engineering for debugging modifications to LGPL components is permitted.
Keep the replacement ABI, architecture, compiler ABI and build configuration
compatible. On Windows, stop the service and replace Qt files under `bin` and
`plugins`. On Ubuntu, stop the systemd unit and replace the private Qt files under
`/opt/qcopilots-websearch/lib` and `plugins`. Keep a backup and run `--Version`,
`--CheckConfig`, the deterministic tests and a service restart afterward.

An administrator needs write access to installed files. Portable ZIP extraction
supports replacement without an administrator. Package upgrades can restore the
distributed library versions, so preserve your modified builds separately.

Sources and notices are downloadable with the same release as the binaries.
See [Qt's distribution guidance](https://www.qt.io/development/open-source-lgpl-obligations)
and the actual license texts in the package. The source archive is not merely a
link to a potentially disappearing third-party download.

## Other components

The native build statically links cpp-httplib (MIT), libcurl (curl license),
c-ares (MIT), Lexbor (Apache-2.0), nlohmann/json (MIT), JSON Schema Validator
(MIT) and zlib (Zlib). Their original notices are installed under `licenses`.
Linux Qt also ships ICU 73.2 libraries, with their Unicode/ICU notices.

Linux uses distribution-managed OpenSSL shared libraries. The DEB records its
system dependencies. Apply Ubuntu security updates and restart the service to
load updated libraries. Windows dynamically links the bundled OpenSSL 3.5.9
libraries under Apache-2.0. Packages include their license and release assets
include the matching fixed-version source archive.

The cpp-httplib 0.66.0 header is modified to bound request-input lifetime and
interrupt owned sockets on shutdown. Its MIT license still applies. Packages
include the modified header, patch script and modification notice under `share`.

Windows packages include the permitted Microsoft Visual C++ runtime DLLs from
the licensed Visual Studio redistribution directory. These DLLs are proprietary
Microsoft components. They are not Apache-licensed. The distribution's NOTICE
and SBOM identify them separately. No Windows system DLLs are copied.

SearXNG (AGPL-3.0-or-later), Caddy (Apache-2.0) and Valkey (BSD-3-Clause) run as
separate services in the team deployment. Their images and licenses are identified
in the deployment manifest. Modified SearXNG deployments must satisfy its own
source-sharing requirements.

The official WiX 7 package has maintenance-fee conditions for qualifying use.
Using the packaging scripts does not purchase a license. Review
[the official package terms](https://www.nuget.org/packages/wix/7.0.1) before
building with that distribution. WiX is build tooling and is not bundled as a
runtime dependency of the installed service.
