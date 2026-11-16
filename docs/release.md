# Release gates

`publish` in the build pipeline creates local packages. It does not upload a
release. A `vMAJOR.MINOR.PATCH` tag triggers CI on that exact commit, including
full stability and live public-search acceptance. Ordinary push/PR builds use
short deterministic checks. Manual CI dispatch can enable the same release checks.
The stability gate runs cold and hot caches for five minutes each on both target
platforms, with five clients and a total load of two requests per second. Request
success, latency, memory, health responsiveness and shutdown thresholds remain
the same. This acceptance duration does not establish multi-hour endurance.

Linux also builds an isolated Clang 21 configuration with ASan and UBSan, runs
all discovered tests, analyzes every first-party C++ translation unit with
clang-tidy, and runs HTML, URL and JSON-RPC fuzz campaigns. Ordinary CI uses
60 seconds per campaign. Full and tag candidates use 300 seconds. The retained
logs, compiler flags, seed hashes and source snapshot are checked at promotion.
Manual dispatch can select `FuzzSeconds: 300` independently to rerun a failed
quality campaign without repeating an already completed stability workload.
This does not turn the short stability evidence into full release acceptance.
The minimal Ubuntu bootstrap check starts from a pinned image without Python.

Windows Server CI does not prove Windows 11 installation behavior. Download the
candidate ZIP and MSI from the successful CI run. Keep their exact bytes. On a
clean Windows 11 desktop, validate the portable ZIP in a fresh path containing
spaces using `scripts/acceptance_windows_zip.py --Package <ZIP> --PackageSHA256
<SHA256> --Output <report.json>`. This runs the official SDK from the moved tree
with only Windows system directories on PATH. Run the full stability command
on the same candidate runtime, and execute
`AcceptWindowsMsi.ps1` from an elevated 64-bit PowerShell session. Supply a lower
version baseline MSI using the same product upgrade identity. The script refuses
pre-existing products, services, installation/data directories or occupied ports.
It exercises installation, failed transactions, upgrade, retained configuration,
service identity, shutdown and uninstall, and cleans only its own state.
The baseline's `build-info-windows-x64.json` must be adjacent to its MSI, or
provided with `--BaselineBuildInfo`. Reports bind the actual baseline package,
source commit and dirty status, PowerShell version and edition, and SHA-256 of
the acceptance script. Local upgrade fixtures may be dirty and are explicitly
recorded as such. They are never promoted as release artifacts. The verifier
requires the script from the tagged release source and a lower baseline version
with the same upgrade identity.

The external evidence JSON is maintained under `release-evidence/` in a later
repository commit. It contains these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | `1` |
| `source_commit`, `candidate_run_id` | Exact tagged source and successful CI run |
| `package_sha256` | Complete Windows ZIP/MSI map copied from candidate build-info |
| `msi_report` | Original `AcceptWindowsMsi.ps1` result JSON |
| `portable_report` | Original `acceptance_windows_zip.py` report, including the official SDK results |
| `soak_report` | Original full cold/hot Windows 11 stability report |
| `delivery_gates` | Recorded human review and QGIS integration signoff |

`delivery_gates` records `council_status: "approved"`, `unresolved_p0_p1: 0`,
the frozen `qgis_commit`, `qgis_launch_stage: "launch4Release"`,
`qgis_release_passed: true`, and `temporary_state_restored: true`.
Final QGIS acceptance uses the published package through the prescribed
`launch4Release` command. Development-launch results are retained separately
as diagnostic history and do not gate release. A recorded development failure
must remain a failure rather than being replaced with a successful startup or
an older page test. These are explicit delivery attestations. They do
not replace the council's original reports, scoring, issue arbitration or QGIS
test records. Preserve those underlying records with the delivery evidence.

Run the **Promote verified release** workflow with the tag, candidate run ID,
evidence commit, evidence path and exact evidence-file SHA-256. It verifies the
run and artifact repository, source commit, workflow identity, artifact digests,
package hashes, per-package inventories, matching sources, CTest receipts, SDK,
full Linux/Windows stability, DEB/MSI acceptance, real Caddy HTTPS behavior,
public search/read/find evidence and delivery signoff.
Each Windows 11 SDK/stability report records its own kernel build and workstation
product type. CI diagnostics must come from the same repository and run attempt.
`FullStability` runs the full Ubuntu cold/hot workload and five-minute fuzz
campaigns. The Windows Server CI job retains its smoke workload, then makes the
candidate available for the required cold/hot Windows 11 desktop acceptance.
Use **Re-run all jobs** if repeating a candidate run, rather than mixing artifacts
from successful and retried matrix jobs.

Promotion never rebuilds the candidate. All required assets are uploaded to a
draft release, downloaded and hashed again, then published together. A failure
leaves the release unpublished. Existing releases are not overwritten or deleted.
The workflow checks the tag again before publication. `promotion-inputs.json`
records the original artifact IDs/digests and external evidence commit. Packages
remain explicitly unsigned until separate signing evidence is provided.

Tag CI generates GitHub artifact attestations for the exact ZIP, MSI and DEB
bytes in a separate job with OIDC permission. PR jobs do not receive signing
permissions. Promotion runs `gh attestation verify` for each package and checks
the certificate's repository identity, source commit, tag, workflow, run ID and
attempt. It preserves the verified bundles in `verified-build-provenance.json`.
This proves build origin. It does not provide Windows Authenticode signing.

Public tests report engine failures and zero results without imposing a minimum
search-success rate. They still require all 60 representative attempts and at
least one real search/open/find closure. Service errors, invalid contracts or
failed cleanup block acceptance. The reference Caddy check uses a synthetic
search provider and is labelled separately from the real SearXNG public report.

The 60-query corpus includes four navigation queries per language with official
domains declared before execution. These requests have no domain or time filter.
Reports retain their first matching official-domain rank, top-1/top-5 counts and
MRR@5, including zero credit for empty/error results. These are domain-level
navigation observations, not a human relevance judgment or a coverage guarantee.
Promotion verifies the exact query corpus and recomputes the recorded metrics.
No ranking-quality threshold is imposed on the best-effort public backend.
