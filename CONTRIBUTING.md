# Contributing

Discuss proposed changes to public contracts or deployment behavior before implementation.
Keep implementation and tests portable across Windows 11 amd64 and Ubuntu 26.04
amd64. Follow the repository's `.clang-format` configuration for C++.

Use the shared build pipeline described in `docs/build.md`. Commit source and
schemas, never downloaded toolchains, credentials, private configuration, caches
or machine-specific build paths. Keep generated reports and review archives out
of version control. Keep dependency versions and verification hashes
together. Changes to dependency versions require both platform builds.

Both MCP transports expose the same versioned tool contract. The QGIS adapter
relays tool discovery and calls to the service. Keep canonical schemas, interface
documentation, contract tests and SHA256SUMS in sync when changing the tool contract.
Do not add test-only bypasses to public-fetch protections.
Prefer injected transports, temporary directories, dynamic ports and controllable
clocks for deterministic tests. Keep public-engine checks separate from the
deterministic suite.

Provide the concrete behavior changed, its reason and relevant test results in
pull requests. Record failures and skipped checks honestly. Production releases
require both platform jobs, installed-binary smoke tests and installer lifecycle checks to succeed. Pushing a version tag starts
the automatic [release workflow](docs/release.md).

Contributions are accepted under the repository's Apache-2.0 license. Keep
third-party notices intact. Contributions to the separate QGIS adapter follow
QGIS's license and contribution process.
