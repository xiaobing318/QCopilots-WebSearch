# Build and publish a release

GitHub Actions builds Windows x64 ZIP and MSI packages on Windows Server 2025 and an Ubuntu 26.04 amd64 DEB. Desktop acceptance uses the ZIP on Windows 11. The Ubuntu job verifies the DEB on Ubuntu 26.04.

## Continuous integration

- `ci.yml` runs on pushes, pull requests and manual dispatch. It also checks punctuation in documentation.
- Reusable `build.yml` builds the TypeScript frontend once with the Node version from `.nvmrc`. The cached native jobs use the same `web-dist` and the checked-in ci build profiles.
- Each native job configures, builds, tests, installs and packages the service. SDK and ZIP or DEB smoke checks run against installed artifacts. A failed native job uploads its CTest results and diagnostics when available.
- Main branch pushes and manual runs add ASan, UBSan, clang-tidy and bounded fuzzing on Ubuntu 26.04.
- Manual and release runs add installer lifecycle and Caddy checks. The installer upgrade baseline is built from the same source at version 0.0.1 to test the package lifecycle.

A feature branch can run the normal workflow without a release tag. CI and the local `publish` pipeline stage produce artifacts only. MSI testing requires a disposable elevated Windows runner.

## Publish from a version tag

Update the version in the root CMake file first. When the intended commit is ready, push a matching `vMAJOR.MINOR.PATCH` tag. `release.yml` verifies the tag and delegates the release build and acceptance to `build.yml`.

The release job uses the tested ZIP, MSI and DEB artifacts. It adds the matching Qt source archive after verifying the digest from `config/dependencies.lock.json`, writes `SHA256SUMS`, and creates GitHub build-provenance attestations. It publishes the assets with `gh release create --verify-tag --generate-notes`.

Verify downloaded files against `SHA256SUMS`. GitHub CLI can verify a distribution package's provenance.

```bash
gh attestation verify ./qcopilots-websearch_<version>_amd64.deb --repo xiaobing318/QCopilots-WebSearch
```

See [license notices and Qt replacement instructions](licensing.md).
