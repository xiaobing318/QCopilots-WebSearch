# Security reporting

Report a suspected vulnerability privately through the repository's GitHub
security advisory reporting interface when available. If unavailable, open a
minimal issue asking for a private reporting channel without exploit details,
credentials or private page content. Do not include production tokens in logs.

Relevant boundaries include caller authentication, exact Host/Origin checks,
client-scoped snapshots, public destination validation, resource limits, private
cache/credential permissions and installer ownership. A private administrator-
configured SearXNG origin is intentionally distinct from untrusted page URLs.

The service returns untrusted web content. It cannot guarantee that an LLM client
will ignore instructions embedded in that content. Consumers must preserve the
distinction between source text and trusted instructions.

Dependency and operating-system security updates require rebuilding or updating
the applicable component and repeating affected tests. See the published release
notes for security fixes. The Release workflow issues build attestations for the
installers and portable archive after installation checks pass.
