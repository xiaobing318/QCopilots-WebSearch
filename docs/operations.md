# Operations and recovery

Use independent credentials per caller. A page snapshot belongs to its client
identity, not merely to a URL. Rotate a token by updating its private file or
environment source and restarting. Keep the identity when retaining its cache
access is intentional. Never commit token files or copy them into diagnostics.

## Capacity and monitoring

Business and control listeners are separate. The default capacity is five
operations. After parsing and authentication, work that exceeds business
operation capacity returns `busy`/429. Control health uses a separate worker
pool. Request input has a two-second absolute read deadline, including chunk
framing and trailers. Sending small amounts of data continuously does not extend
that deadline. Application operations use their own deadlines. Increase capacity
only after measuring memory, latency and upstream limits at the new setting.

The authenticated control listener provides `/health/live`, `/health/ready` and
Prometheus text `/metrics`. Current gauges include `qcws_active_calls` and
`qcws_active_upstream`. Status also reports waiting coalesced searches and the
last observed dependency state. These endpoints do not perform a public search.
Readiness of this process is not proof that Google, Bing or another upstream is
currently reachable.

Normal logs exclude tokens, raw queries and page bodies. Operators should also
avoid enabling access logs that capture credentials or request bodies at their
reverse proxy. Monitor latency, 429s, provider failures and package/service restart
events using bounded retention. Container log rotation is configured in Compose.

## Cache and backups

Search TTL defaults to five minutes, page TTL to thirty minutes. The default
cache budget is 256 MiB and 10,000 entries. SQLite is owned by a dedicated thread
and the cache directory is locked against a second process. Corrupt, inaccessible
or unsupported cache state fails explicitly rather than silently discarding data.

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

## Upgrades

Windows packages dynamically link OpenSSL using the matched `libssl-3-x64.dll`
and `libcrypto-3-x64.dll` pair beside the executable. Prefer updating the complete
validated package. For an administrator-managed library replacement, stop the
service, preserve the previous pair, use compatible matching x64 builds, and
repeat TLS and service checks before resuming traffic. MSI repair or upgrade can
restore package-owned DLLs.

Ubuntu OpenSSL remains distribution-managed. Apply distribution security updates
and restart the service. Keep CA trust sources current on both platforms. Trust
store updates do not constitute an online revocation check by this service.

Back up private state before upgrading. Apply the complete validated package,
then check version, service identity, authentication, health and one search/read
workflow. DEB upgrades retain dpkg-managed configuration and generated credentials.
Windows MSI uses a stable upgrade identity and transactional initialization. Its
rollback receipt records file identity and content hash, allowing rollback to
remove only unmodified files created by that installation. A conflicting receipt
or modified file is preserved for administrator inspection.

Windows commit cleanup failure leaves a private `.qcws-install-*.json` receipt.
Do not blindly delete its referenced files. Inspect the installer log and recorded
transaction first. Normal successful installation leaves no such receipt.

Debian package operations are not an automatic transactional snapshot. If a
maintainer script fails, dpkg can report `half-configured`. Correct the cause and
configure the package again, or reinstall the exact known-good baseline package
with `--allow-downgrades`. Validate configuration compatibility before downgrading.
The acceptance report labels this as recovery by baseline reinstall.

The Windows acceptance-only `QCWS_TEST_FAILPOINT` MSI property deliberately fails
after initialization or after service start. It is inert by default and requires
an administrator explicitly passing the test value. Never set it in deployment
automation. It permits rollback tests using exactly the candidate MSI bytes.

Removing a DEB retains its conffile and generated state. Purging removes the
dpkg conffile but deliberately retains generated tokens, data and the service
account. This prevents accidental data loss and UID reuse. Remove those retained
objects only through an explicit administrative reset after backups.

`docker compose down` keeps persistent certificates and data. Do not use `down -v`
as a routine upgrade or uninstall step. Keep Caddy certificate storage and the
SearXNG secret stable across restarts.

## Limits of public retrieval

Public requests allow HTTP(S) only, validate actual connection addresses and
redirect destinations, refuse HTTPS downgrade and block non-public addresses.
They do not inherit a system proxy. The administrator's fixed SearXNG endpoint
has a separate policy so a private backend can be used safely.

Compressed transfer and decoded body each have a five-MiB bound. HTML parsing has
additional bounded input, node, depth, text and link budgets. Parser truncation is
reported. Exceeding transfer limits is an error. Extraction does not execute
JavaScript, and web text remains untrusted content for downstream agents.
