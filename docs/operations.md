# Operations and recovery

Use independent credentials per caller. A page snapshot belongs to its client
identity. Rotate a token with the CLI or by updating an existing private file
or environment source, then restart. Keep the identity to retain its cache
access. Keep token files out of source control and diagnostics.
[Client token management](deployment.md#manage-client-tokens)

## Capacity and monitoring

Business, control and settings listeners are separate. The default capacity is five
operations. After parsing and authentication, work that exceeds business
operation capacity returns `busy`/429. Control health uses a separate worker
pool. Request input has a two-second absolute read deadline, including chunk
framing and trailers. Sending small amounts of data continuously does not extend
that deadline. Application operations use their own deadlines. Increase capacity
only after measuring memory, latency and upstream limits at the new setting.

The authenticated control listener provides `/health/live`, `/health/ready` and
Prometheus text `/metrics`. Current gauges include `qcws_active_calls` and
`qcws_active_upstream`. Status also reports waiting coalesced searches and the
last observed dependency state. That state is `degraded` when some engines
failed or an error followed partial results, and `unavailable` only when an
error left no results. Client cancellation does not change it. These endpoints
do not perform a public search.
Readiness of this process is not proof that Google, Bing or another upstream is
currently reachable.

Normal logs exclude tokens, raw queries and page bodies. Operators should also
avoid enabling access logs that capture credentials or request bodies at their
reverse proxy. Monitor latency, 429s, provider failures and package/service restart
events using bounded retention. Container log rotation is configured in Compose.

## Manage search sources

Open the bilingual settings page using `--SettingsUrl`. A management link expires
after 120 seconds and can be used once. Clients should use distinct business
tokens. MSI allows local interactive users to read the separate `admin.token`.
On Ubuntu the admin token is service-owned with mode 0600. Keep management links,
tokens and provider secrets out of logs.

Auto uses public DuckDuckGo and Bing pages and can report `provider_unavailable`
or `rate_limited` when they are unreachable or throttling. Use the provider test
in Settings to diagnose a failure. For managed search, select your SearXNG
endpoint or configure a Brave, Tavily, Exa, Perplexity or DeepSeek key.

## Cache and backups

New configurations default to a thirty-minute TTL for search and page snapshots. Partial search
results are kept for at most 30 seconds, and partial results without hits are
not cached. The default cache budget is 256 MiB and 10,000 entries. SQLite is
owned by a dedicated thread and the cache directory is locked against a second
process. Corrupt, inaccessible or unsupported cache state fails explicitly rather
than silently discarding data. The one exception is a search-cache write, whose
failure only leaves that result uncached.

Use healthy local storage for the cache. Shutdown timing cannot cover an
indefinitely stalled operating-system filesystem. Forced termination can leave
temporary state behind. Remove such state only after confirming its owning
process has exited and the directory belongs to that instance.

Cache hits record their latest access order in bounded memory. The next content
write applies those updates in its existing SQLite transaction before choosing
an eviction victim. Normal shutdown checkpoints the remaining access metadata.
A crash or failed shutdown checkpoint can lose recent access order, but does not
change committed page content or expiry times. Reads do not issue a synchronous
disk commit for each hit.

Stop the service before taking a filesystem backup of configuration, credentials
and cache. Protect backups with the same access controls as the originals.
Do not copy a live SQLite file while ignoring its journal. Cache data can be
recreated, but losing it invalidates page IDs. Keep configuration and credentials
when replacing cache state.

For a cache failure, stop the service, preserve the failing directory for
diagnosis, create a fresh private directory and update its configuration path.
Run `--CheckConfig`, restart and verify health. Do not automatically overwrite a
corrupt or locked cache. Repair filesystem permissions or disk capacity first.

## Install, switch and remove versions

Runtime configuration uses `schema_version: 1` for its JSON format.
The `project_version` field records the exact release that generated the
configuration, such as `"0.2.1"`. A build rejects a stamped configuration from
a different release. Run `--CheckConfig` with the executable for the version
you want to check. It reads that executable's adjacent `data/runtime.json`
by default. Run this check as administrator for MSI and with `sudo` for DEB.
Explicit `--ConfigPath` continues to accept legacy files without `project_version`.

MSI and DEB register a separate service and program directory for each version.
Each service loads `bin/data/runtime.json` from its own installation root.
Every new version gets fresh credentials, provider settings, cache and
loopback ports. The existing service keeps running while the new one is
installed.

To switch clients, install the complete target package. Verify the version,
service identity, authentication, health, and one search and page read. Update
the client endpoint and token together. Page IDs belong to the service and
client that created them. Open the URL again on the new version. To switch back,
restore the clients' earlier endpoints and credentials while that service
is still available. [Version-specific paths and ports](deployment.md#installed-versions-and-ports)

Windows uses system Schannel. Ubuntu uses the distribution's OpenSSL shared
libraries. Apply OS security updates and restart the service. Maintain the
appropriate CA stores and supply complete certificate chains to endpoints.

New installations use the automatic search source. Select any private SearXNG
endpoint or configure a provider key using that version's Settings page.
Browser settings and key changes take effect on save. External edits are
checked every two seconds. If a file is invalid, the admin status reports
`settings_error` while the last valid snapshot stays active.

Back up private state before changing or removing a deployment. Use matching
configuration and executable versions when restoring. Windows MSI initializes
each version in its own installer transaction. The rollback receipt records file
identities and content hashes. Rollback only removes unmodified files created
by that transaction. A conflicting receipt or modified file is preserved for
administrator inspection.

Windows commit cleanup failure leaves a private `.qcws-install-*.json` receipt.
Do not blindly delete its referenced files. Inspect the installer log and recorded
transaction first. Normal successful installation leaves no such receipt.

If a DEB maintainer script fails, dpkg can report `half-configured`. Correct the
cause and configure that package again. A separately installed earlier version
remains available during recovery.

The Windows test-only `QCWS_TEST_FAILPOINT` MSI property deliberately fails
after initialization or after service start. It is inert by default and requires
an administrator explicitly passing the test value. Never set it in deployment
automation. It permits rollback tests using exactly the candidate MSI bytes.

Select the exact version to remove. On Windows, uninstall the corresponding MSI
entry in Installed apps. On Ubuntu, use the package name including its version,
for example `sudo apt remove qcopilots-websearch-0.2.1`. Both uninstall paths
retain that version's `bin/data` directory. A DEB purge removes the version's
`bin/data/runtime.json` and retains its tokens, state and service account.
Remove retained files only when intentionally resetting that version after
backups.

`docker compose down` keeps persistent certificates and data. Do not use `down -v`
as a routine upgrade or uninstall step. Keep Caddy certificate storage and the
SearXNG secret stable across restarts.

## Limits of public retrieval

Public requests allow HTTP(S) only, validate actual connection addresses and
redirect destinations, refuse HTTPS downgrade and block non-public addresses.
They do not inherit a system proxy. The administrator's fixed SearXNG endpoint
has a separate policy so a private backend can be used safely.

A host whose resolver answers with non-public addresses cannot read any page.
`web_open` then returns `destination_blocked` with "URL host resolves only to
non-public addresses." for every URL. A common cause is a TUN-mode proxy in
fake-IP mode, which answers DNS with addresses from `198.18.0.0/15`. Run the
service on a host with ordinary DNS, or exclude its process or its DNS queries
from fake-IP mapping. The service never treats these ranges as public.

Compressed transfer and decoded body each have a five-MiB bound. HTML parsing has
additional bounded input, node, depth, text and link budgets. The parser stops as
soon as a budget is exhausted, so hostile markup cannot multiply nodes in memory.
Parser truncation is reported and the readable part is still returned. Exceeding
transfer limits is an error. Extraction does not execute
JavaScript, and web text remains untrusted content for downstream agents.
