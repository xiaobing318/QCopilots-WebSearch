# v0.1.4 accepted candidate evidence

The candidate is tag v0.1.4 at source commit3e13f7bd930ffa495e14d50c3556028a21484204, CI43805306130 attempt1. The later evidence commit does not change the package source or rebuild any artifact.

- [Final council report](council-report.md): actual three-seat votes, scope limitations, tests and residual risks.
- [Windows11 release evidence](windows11.json): original MSI/ZIP/300-second cold/hot reports, actual disposition and review-audit SHA256.
- [Original review audit](review-audit.zip): immutable raw logs, samples, screenshots, provenance, source manifests, original final votes and diagnostic records. It retains CRLF and the pre-vote pending envelope unchanged. Unpack and follow its README path mapping to verify original index hashes.
- [Separate GPL QGIS adapter](https://github.com/xiaobing318/QGIS/commit/f69eeed3fe141c13bdc0339aa3ee43ea3c3d6c4d).

Release promotion still performs exact CI artifact/provenance verification and draft upload/download byte comparison. Only a published Release is an accepted distribution. Packages are unsigned. Public search is best effort. Embedded QGIS first-load white-screen recovery does not establish a root-cause fix. See the detailed council and diagnostic records.


## Completed publication and download display

[Publication run 43823293370](https://github.com/xiaobing318/QCopilots-WebSearch/actions/runs/43823293370)
completed successfully and made v0.1.4 public on 2026-11-16 at 07:23:23 UTC.
All 80 original physical assets were downloaded and verified before publication.
[Publication verification](publication-verification.json) and
[raw publication audit](publication-audit.zip) retain that historical state.

The user subsequently requested a concise download list and selected one combined
corresponding-source archive. The intended list is ZIP, MSI, DEB, the combined
source ZIP and SHA256SUMS. [Display plan](display-plan.json) maps every one of the
original 80 assets to its preserved location. The three source archives retain
their original bytes inside the combined source ZIP. The other 74 files,
including all logs, JSON, SBOMs, original manifests and original checksums, are
preserved byte-for-byte in [release-assets-audit.zip](release-assets-audit.zip).
The three installation package bytes are unchanged. This is a download-display
adjustment after successful publication, not a new build or a bypass of the
original acceptance gates. The completed [display receipt](display-result.json) records five uploaded assets. The source bundle was downloaded in full and every member verified before deletion. [Display audit](display-audit.zip) preserves the operation, checksum replacement and independent fact checks. GitHub-generated source ZIP/TAR links are separate from these five uploaded assets.

[R4 review](council-r4-report.md), [raw R4 audit](review-r4-audit.zip) and
[successful CheckOnly receipt](publication-checkonly.json) document the separately
reviewed publication-tool correction. The user authorized three independent
Codex seats in R4 and requested no further Claude invocation. The accepted P2
publication-response ambiguity and its operational handling are explicit there.
