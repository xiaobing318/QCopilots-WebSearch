# Version 1 interfaces

All business endpoints require `Authorization: Bearer <client-token>`. Every
client has an independent identity. Host authorities and browser Origins must
match exact configured entries. An absent Origin is accepted for native clients.
Credentials are not supplied in URLs. Remote clients use verified HTTPS.

| Interface | Request |
| --- | --- |
| REST search | `POST /v1/search` with a `web_search` input object |
| REST reading | `POST /v1/open` with a `web_open` input object |
| REST find | `POST /v1/find` with a `web_find` input object |
| REST status | `GET /v1/status` |
| MCP HTTP | JSON-RPC POST to `/mcp`, protocol `2025-06-18` |
| MCP stdio | `--Stdio --ConfigPath FILE`, newline JSON-RPC |

The canonical input schemas are in `contracts/v1`. MCP `tools/list` embeds those
same schemas. The QGIS adapter carries identical copies and validates requests
before sending them to the fixed REST backend.

## Search

```json
{"query":"QGIS spatial index", "limit":10, "language":"en", "domains":["qgis.org"], "time_range":"any"}
```

`limit` defaults to 10 and is at most 20. The service scans at most three upstream
pages internally. Search does not expose a page cursor. Bounded scanning means strict domain filtering can return fewer
results than requested. Hostname normalization and domain/subdomain matching are
enforced locally after upstream search. Similar suffixes such as
`notexample.org` do not match `example.org`.

`time_range` accepts `any`, `day`, `month`, `year`. It is an upstream hint and
does not guarantee strict publication-date filtering. Results contain title,
URL, snippet, sources and nullable publication date. Search results can be
`ok` or `partial`. All upstream engines failing is an explicit error, distinct
from a successful empty result. Failures, warnings, fetched time and cache state
remain visible to the caller.

## Reading and finding

`web_open` accepts exactly one of `url` and `page_id`. Only public HTTP(S) HTML
or text is fetched. It returns a bounded snapshot with line numbers, links,
provenance, truncation flags and a client-scoped page ID. The default starts at
line 1 with 200 lines, at most 500. Continue using `next_start_line` and the
same page ID rather than refetching changing content.

`web_find` searches a cached snapshot using literal text. Case-insensitive
Unicode matching is the default. There are no regular expressions. It returns
matching lines, at most 20 by default and 100 when requested. Unknown, expired
or another client's page ID returns `page_unavailable`.

Reading does not run JavaScript, follow instructions embedded in pages, extract
PDFs or generate an LLM answer. Treat returned text as untrusted evidence.

Text decoding selects a BOM first, then the HTTP charset, an early HTML meta
declaration, and finally UTF-8. Lexbor supports UTF-8/UTF-16, Windows-1252,
GBK/GB18030 and Big5 among its encodings. Unsupported encodings and malformed
encoded text return `unsupported_content` rather than silently replacing bytes.

| Resource | Limit |
| --- | --- |
| Compressed transfer and decoded body | 5 MiB each |
| HTML response bytes before decoding | 1 MiB |
| HTML markup tokens and visited nodes | 50,000 each |
| HTML traversal depth | 512 |
| Extracted text | 200,000 Unicode characters, 10,000 lines |
| Line length | 1,000 Unicode characters |
| Extracted links | 200 |
| Service payload | 240 KiB |
| Complete REST or MCP response | 1 MiB |

HTML input, extracted text and response truncation are disclosed. Exceeding
transfer or HTML complexity budgets returns `response_too_large`. Open/find
reduce returned lines and links to fit the payload budget. `next_start_line`
continues omitted lines or matches, not omitted links.

## Results and errors

Successful tool payloads include `contract_version: 1`. MCP returns the same
payload as both `structuredContent` and one JSON text content block. Business
failures use `isError: true`. Invalid tool names or input shapes use JSON-RPC
invalid-params errors. No `outputSchema` is advertised in this version.

JSON input is limited to 1 MiB and 64 nested containers. Excessive nesting is
rejected before building the JSON tree. Serialized request IDs (including JSON
quotes and escapes) are limited to 64 KiB. Larger IDs are outside this service's
accepted MCP profile and receive Invalid Request with `id: null`, without creating
a session or running a tool. Clients must keep IDs within the limit for request
correlation. Responses to accepted IDs preserve the ID and use a JSON-RPC error
if the complete reply exceeds 1 MiB. Stdio responses share the 1 MiB response
budget and use a bounded queue of at most 32 messages and 4 MiB, including the
active write. Closing stdin cancels a blocked stdout writer after a short drain
period, so a client that stops reading cannot prevent process cleanup.

REST errors use this envelope and a suitable HTTP status:

```json
{"error":{"code":"busy","message":"Service capacity is exhausted.","retryable":true}}
```

Errors do not have a success `contract_version` field. Clients should branch on
HTTP status or MCP error type before parsing successful content.

| Code | Typical HTTP status | Meaning |
| --- | --- | --- |
| `invalid_argument` | 400 | Input violates the versioned schema |
| `unauthorized` | 401 | Missing or invalid independent credential |
| `destination_blocked` | 403 | Public fetch policy rejected a target |
| `page_unavailable` | 404 | Snapshot is unavailable to this identity |
| `request_too_large` | 413 | Incoming request exceeds the limit |
| `unsupported_content` | 415 | Unsupported media type, charset or malformed encoded text |
| `busy` | 429 | No business/upstream capacity available |
| `cancelled` | 499 | Request work was cancelled |
| `response_too_large` | 502 | Upstream transfer exceeded bounded limits |
| `provider_unavailable` | 502 | Search dependency could not supply results |
| `cache_unavailable` | 503 | Private cache cannot be used safely |
| `timeout` | 504 | Operation deadline expired |

MCP HTTP requires initialization, the returned session ID and negotiated
protocol header on later requests. Send `notifications/initialized`, then call
tools. DELETE terminates the session. GET returns 405 because no unsolicited
SSE stream is provided. Cancelling a request cancels its work without affecting
another coalesced caller that is still waiting.

HTTP sessions expire after 30 idle minutes, with a maximum of 100 globally and
20 per caller identity. At capacity, only the requesting identity's oldest idle
session without active requests can be evicted. If none is eligible,
initialization returns 503. Request history is limited to 4,096 IDs and 1 MiB.
An expired or retired session returns 404 and the client reinitializes.

Stdio follows the child-process lifetime and retains a bounded recent ID window
plus all active IDs, without a lifetime request-count quota. Clients must still
use unique request IDs throughout a session.
