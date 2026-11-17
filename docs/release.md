# Release workflow

Pushing a `vMAJOR.MINOR.PATCH` tag runs the Windows and Ubuntu build matrix,
automated tests, package checks and build attestations, then publishes a GitHub
Release. The tag must match the project version. Branch and pull-request jobs
do not publish. Manual dispatch runs checks only, even when a tag is selected.
The local build pipeline's `publish` stage creates packages without uploading
them.

## Required checks

Both platform jobs build, run CTest and the official MCP SDK tests, and package
the same source commit. Windows CI uses Windows Server 2022 and Ubuntu CI uses
Ubuntu 26.04. The Windows job checks the final ZIP from a relocated directory
with only system directories on PATH. It also tests MSI installation, failed
transactions, upgrade, configuration preservation, service identity, shutdown
and uninstall on the disposable runner. This is automated Windows Server
coverage, not a claim that Windows 11 desktop behavior has been exercised.

The Linux job tests DEB installation, upgrade and recovery by baseline reinstall,
Caddy HTTPS behavior, and bootstrap in a minimal Ubuntu image without Python.
Installer baselines use version `0.0.1` built from the same source. They are
test fixtures rather than earlier supported releases.

Tags run cold and hot stability phases for 300 seconds each on both platforms.
The controlled 100 ms upstream fixture serves five clients at a total of two
requests per second. Each phase requires all normal requests to succeed,
business p95 latency at most one second, health p95 at most 200 ms, RSS at most
512 MiB and process shutdown within ten seconds. This duration does not establish
multi-hour endurance. Ordinary jobs use ten-second smoke phases. Manual
`FullStability` enables the full phases on both platforms.

Linux quality checks build with Clang 21, ASan and UBSan, run all discovered
tests, analyze every first-party C++ translation unit with clang-tidy, and fuzz
HTML, URL and JSON-RPC parsing. Instrumentation, test coverage and actual fuzz
duration are checked during execution. Ordinary CI uses 60 seconds per campaign.
Tags and `FullStability` use 300 seconds. Manual dispatch can also select
`FuzzSeconds: 300` without enabling the longer stability workload.

Tags run the real public-search check through the pinned SearXNG deployment.
Manual dispatch can enable it with `LivePublicSearch`. All 60 fixed queries
(30 Chinese and 30 English) must be attempted, with at least one successful
search/open/find flow. Engine failures and empty results remain visible without
a minimum search-success-rate requirement. Service errors, invalid contracts
or failed cleanup fail the check. Upstream availability can therefore prevent
a tag's checks from completing successfully.

The query corpus includes four navigation queries per language with official
domains declared in advance and no domain or time filter. The check computes
first matching domain rank, top-1/top-5 counts and MRR@5, with zero credit for
empty or failed results. These observations are not human relevance judgments
and impose no ranking-quality threshold. The Caddy test uses a controlled local
provider and does not stand in for this public-search check.

## Distribution files

The public Release contains ten assets:

| Contents | Count |
| --- | --- |
| Windows ZIP and MSI, Ubuntu DEB | 3 |
| CycloneDX SBOM for each package | 3 |
| Project source, matching Qt source and Windows OpenSSL source archives | 3 |
| Combined `SHA256SUMS` | 1 |

Package names and matching source archive names are retained. Licenses,
third-party notices and Qt shared-library replacement instructions remain in
the installation packages. Windows packages are unsigned. GitHub build
attestations establish build origin and do not provide Authenticode signing.

Build and test receipts remain useful locally for rejecting stale source,
profile or installation state before packaging. Internal package metadata also
passes between CI jobs. Neither those files nor test reports, screenshots,
review records and audit archives are public Release assets or committed source.
CI uses temporary directories for test output and cleans up its owned processes,
containers and temporary state. Normal console logs remain subject to GitHub
Actions retention. Platform package artifacts are retained for 14 days for
cross-job transfer and retries.

## Verification and publication

The publishing job depends on both platform jobs and the attestation job.
It resolves the two platform artifacts in the current workflow run, fixes their
immutable IDs and digests, and downloads those exact artifacts. Verification
checks the repository, source commit, tag version, package formats, checksums,
SBOM bindings, package inventories and matching dependency sources. Windows
ZIP and MSI payloads must agree. Local dirty builds cannot be published.

`gh attestation verify` checks each distribution package against its exact
SHA-256, repository identity, source commit, tag and CI workflow identity.
The attestation must belong to the same workflow run. Successful jobs from an
earlier attempt of that run can be reused when only failed jobs are retried.
The publishing job does not rebuild packages.

The publisher creates a draft or reuses a draft with the same tag and exact
source commit. Existing assets are downloaded and hashed before any upload.
Matching files are reused and only missing files are uploaded. Unexpected assets,
different bytes, a changed tag or incomplete `starter` uploads stop the operation.
Existing Release assets are never overwritten or deleted by the publisher.

After uploading, the publisher downloads and verifies the complete asset set
again before making the draft public. Failures before publication leave a draft
for inspection or a safe retry. A retry against an already published Release
succeeds only when the full asset set and bytes match. If a publication response
is lost, the publisher checks the remote state before reporting its outcome.
This flow applies to tags containing the current workflow. Older tags and
existing Releases are not migrated.

## Publisher command

The CI job invokes:

```text
python scripts/publish_verified_release.py --ArtifactsRoot <new-download-directory> --Tag vX.Y.Z --Commit <40-character-commit> --Output <new-release-directory>
```

Both directories must be new and separate. The publisher downloads artifacts
itself. It requires `GH_TOKEN`, `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`,
`GITHUB_RUN_ATTEMPT`, `GITHUB_SHA`, `GITHUB_REF` and `GITHUB_EVENT_NAME` (which
must be `push`), plus a clean checkout of the tagged commit. `--CheckOnly`
verifies inputs and any existing remote assets without creating, uploading,
deleting or publishing remote objects.

The release and asset operations use the [GitHub Release API](https://docs.github.com/en/rest/releases/releases)
and [asset API](https://docs.github.com/en/rest/releases/assets).
