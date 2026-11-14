# Runtime architecture

REST, HTTP MCP and stdio share the same service and versioned tool contracts.
HTTP business and control listeners have separate bounded worker pools. The
five-operation business gate is shared by REST and HTTP MCP. Each stdio process
has its own service instance and capacity.

Outbound HTTP(S) uses libcurl with c-ares and OpenSSL. Public retrieval validates
each actual connection address before connecting and checks every redirect.
The administrator-configured SearXNG endpoint has a separate policy that permits
a private backend. Neither connection path inherits system or environment
proxies. The build pipeline's download proxy does not configure runtime traffic.

TLS verifies the certificate chain, validity period, server purpose and URL
hostname. Windows imports certificates from the native ROOT and CA stores when
no explicit backend CA file is configured. Partial chains are rejected. This
uses OpenSSL verification and does not reproduce every Schannel policy.
An explicit backend CA file replaces the default trust sources for that request.
Ubuntu otherwise uses its distribution-managed CA configuration.

Certificate verification does not retrieve AIA issuers, CRLs or OCSP responses.
Missing intermediate certificates therefore fail verification unless supplied
by the server or already available from the selected trust sources. Version 1
does not provide online revocation checking. OpenSSL configuration files are
not automatically loaded.

A hash-checked local cpp-httplib patch keeps the two-second request-input deadline
active through headers, body, chunk framing and trailers. The deadline ends
before application work begins. Control endpoints and non-POST routes reject
request bodies and close the connection. Shutdown interrupts accepted sockets,
including sockets waiting in the bounded worker queue.
