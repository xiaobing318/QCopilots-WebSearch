# Version 1 interfaces

All business endpoints require `Authorization: Bearer <client-token>`. The
`Bearer` scheme name is case-insensitive. Every client has an independent
identity. Host authorities and browser Origins must match exact configured
entries. An absent Origin is accepted for native clients. Credentials are not
supplied in URLs. Remote clients use verified HTTPS.

| Interface | Request |
| --- | --- |
| REST search | `POST /v1/search` with a `web_search` input object |
| REST reading | `POST /v1/open` with a `web_open` input object |
| REST find | `POST /v1/find` with a `web_find` input object |
| REST status | `GET /v1/status` |
| MCP HTTP | JSON-RPC POST to `/mcp`, protocol `2025-11-25` or `2025-06-18` |
| MCP stdio | `--Stdio --ConfigPath FILE`, newline JSON-RPC |

The canonical input schemas are in `contracts/v1`. `contracts/v1/tools.json`
holds each tool's title, description and annotations plus the MCP
`instructions` text. MCP `tools/list` publishes exactly these files. The input
schemas use plain draft-07 objects without top-level `oneOf`, `anyOf`, `allOf`
or `not`, because several model APIs reject such tool schemas. The QGIS adapter
carries identical copies of all five files, checks the `web_open` choice between
`url` and `page_id` itself and validates requests before sending them to the
fixed REST backend.

## Search

```json
{"query":"QGIS spatial index", "limit":10, "language":"en", "domains":["qgis.org"], "time_range":"any"}
```

`limit` defaults to 10 and is at most 20. Repeated whitespace in the query is
collapsed before searching and caching. The service scans at most three upstream
pages internally and does not expose a page cursor. Scanning stops at the limit,
on an empty page, or on a page that adds no URL not seen before. Strict domain
filtering can therefore return fewer results than requested. Hostname
normalization and domain/subdomain matching are enforced locally after the
upstream search. Similar suffixes such as `notexample.org` do not match
`example.org`.

Duplicates across upstream pages are detected while ignoring the scheme, a
leading `www.`, a trailing slash, the fragment, a default port and `utm_*`,
`gclid` or `fbclid` parameters. The first URL returned is kept.

`time_range` accepts `any`, `day`, `week`, `month` and `year`. It is an upstream
hint and does not guarantee strict publication-date filtering. Each result has
`title`, `url`, `snippet`, `sources` and `published_at`. `published_at` is a UTC
ISO-8601 time such as `2024-05-01T10:30:00Z`, or `null` when the provider gave no
usable date. The response also carries up to five `corrections` and five
`suggestions` from the provider, each at most 200 characters.

Search results are `ok` or `partial`. All upstream engines failing is an explicit
error, distinct from a successful empty result. Each failed engine appears once
in `failures` as `{"engine", "reason": "unresponsive", "detail"}`, where `detail`
is the provider's reason of at most 200 characters. When a later page fails or
times out after earlier pages produced results, those results are returned as
`partial` with a `{"engine": "provider", "reason": <error code>}` entry. Each call
makes one upstream attempt per page and does not retry. Warnings, fetched time
and cache state remain visible to the caller.

`cache.ttl_seconds` is the configured search TTL for `ok` results and at most 30
seconds for `partial` results with hits. A `partial` result without hits is not
cached and reports 0. A failed cache write leaves the result uncached without
failing the search. `cache.hit` is also true when a request receives the result
of an identical search that has just finished.

## Reading and finding

`web_open` accepts exactly one of `url` and `page_id`. Supplying both or neither
returns `invalid_argument`. Only public HTTP(S) resources are fetched. It returns
a bounded snapshot with numbered lines, links, provenance, truncation flags and a
client-scoped page ID. The default starts at line 1 with 200 lines, at most 500.
Continue using `next_start_line` and the same page ID rather than refetching
changing content. `next_start_line` is `null` after the last line.

Each response lists only the links whose text starts inside the returned line
window, so continuation reads do not repeat earlier links. A link is
`{"url", "text", "line"}`. `text` is the anchor text with whitespace collapsed,
at most 200 characters and possibly empty. `line` is the 1-based line where that
text starts. Fragments are removed. A repeated URL keeps its first entry, unless
that entry had no text and a later one does. Links to the page itself or to
non-HTTP(S) targets are skipped. A page keeps at
most 200 links and `links_truncated` reports when that cap was reached.

`web_find` searches a cached snapshot using literal text. Case-insensitive
Unicode matching is the default. There are no regular expressions. It returns
matching lines, at most 20 by default and 100 when requested. Unknown, expired
or another client's page ID returns `page_unavailable`. Open the URL again to get
a new snapshot.

Reading does not run JavaScript, follow instructions embedded in pages, extract
PDFs or generate an LLM answer. Treat returned text as untrusted evidence.

### Content and text format

`text/html` and `application/xhtml+xml` are read as HTML. Other `text/*` types,
`application/json`, `application/*+json`, `application/xml` and
`application/*+xml` are read as plain text. Without a Content-Type the body is
sniffed for HTML markers and then for binary bytes. Anything else returns
`unsupported_content`.

HTML text uses light Markdown so that agents can see the page structure.

- Headings start with `#` to `######`.
- List items start with `- `, and ordered items with `N. ` honouring `start`.
  Nested lists indent two spaces per level, up to eight levels.
- Table cells in one row are joined with ` | `, one row per line.
- `pre`, `textarea`, `listing`, `xmp` and plain-text resources keep indentation
  and tabs. Elsewhere whitespace is collapsed.
- Markers appear only when content follows, and links are not inlined.

The readable root is `<main>`, otherwise a single `<article>`, otherwise
`<body>`. `header` and `footer` are dropped only outside `article`, `section`,
`main` and `aside`. The ARIA roles `banner` and `contentinfo` follow the same
rule, and `role="navigation"` is skipped like `nav`. `script`, `style`,
`noscript`, `template`, `nav`, `svg`,
`iframe`, `object`, `embed`, `canvas`, `video`, `audio`, elements with `hidden`
(except `until-found`), `aria-hidden="true"` and inline `display:none` or
`visibility:hidden` are skipped. The title comes from `<title>`, then
`og:title`, then the first `h1`, then the URL.

Control characters and invisible formatting characters that can hide text from a
reader are removed from text and titles. These are U+00AD, U+200B, U+2060,
U+202A to U+202E, U+2066 to U+2069 and U+E0000 to U+E007F. U+FEFF, U+200C and
U+200D are kept because real scripts and emoji need them.

Text decoding selects a BOM first, then a supported HTTP charset, then a
supported HTML meta declaration in the first 4 KiB, and finally UTF-8. Unknown
charset labels are ignored. Lexbor supports UTF-8/UTF-16, Windows-1252,
GBK/GB18030 and Big5 among its encodings. Malformed bytes become U+FFFD. Only
when replacements exceed 1% of the decoded text, or a UTF-32 BOM is present,
does reading return `unsupported_content`.

| Resource | Limit |
| --- | --- |
| Compressed transfer and decoded body | 5 MiB each |
| Encoded bytes read for text extraction | 1 MiB |
| HTML parser | 100,000 inserted nodes, 512 open elements, 1,000 active formatting elements |
| HTML traversal | 100,000 nodes, depth 512 |
| Extracted text | 200,000 Unicode characters, 10,000 lines |
| Line length | 1,000 Unicode characters |
| Extracted links | 200 |
| Service payload | 240 KiB |
| Complete REST or MCP response | 1 MiB |

Only the transfer limits return `response_too_large`. Larger inputs, parser and
traversal budgets and text limits keep the readable part and set `truncated`.
Open/find reduce returned lines and links to fit the payload budget.
`next_start_line` then continues the omitted lines or matches.

## Results and errors

Successful tool payloads include `contract_version: 1`. New fields are added
without changing that version. MCP returns the same payload as both
`structuredContent` and one JSON text content block. Business failures and
arguments that violate the input schema use `isError: true` with the error
object below, so a model can correct its call. The message names the first
failing field, for example `Input does not match the schema at /limit: instance
exceeds maximum of 20.` REST returns the same message with HTTP 400. Only an
unknown tool name is a JSON-RPC invalid-params error. No `outputSchema` is
advertised in this version.

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
HTTP status or MCP error type before parsing successful content. `retryable`
tells the client whether the same call can succeed later. HTTP 429 responses
also carry `Retry-After: 1`.

| Code | Typical HTTP status | Retryable | Meaning |
| --- | --- | --- | --- |
| `invalid_argument` | 400 | no | Input violates the versioned schema or the url/page_id choice |
| `unauthorized` | 401 | no | Missing or invalid independent credential |
| `forbidden` | 403 | no | Host or Origin is not in the configured allowlist |
| `destination_blocked` | 403 | no | Public fetch policy rejected a target, including an HTTPS to HTTP redirect |
| `page_unavailable` | 404 | no | Snapshot is unavailable to this identity |
| `request_too_large` | 413 | no | Incoming request exceeds the limit |
| `unsupported_content` | 415 | no | Unsupported media type, charset or mostly malformed encoded text |
| `busy` | 429 | yes | No business/upstream capacity available |
| `rate_limited` | 429 | yes | The search provider answered with HTTP 429 |
| `cancelled` | 499 | no | Request work was cancelled |
| `internal_error` | 500 | no | Unexpected local failure |
| `response_too_large` | 502 | no | Upstream transfer exceeded bounded limits |
| `provider_unavailable` | 502 | yes | Search dependency could not supply results |
| `upstream_http_error` | 502 | for 429, 5xx and connection failures | A web page could not be read. The message gives the HTTP status or a safe reason such as an unresolved host name |
| `backend_unavailable` | 503 | yes | The service is stopping |
| `cache_unavailable` | 503 | yes | Private cache cannot be used safely |
| `timeout` | 504 | yes | Operation deadline expired |

MCP HTTP requires initialization, the returned session ID and negotiated
protocol header on later requests. `initialize` echoes `2025-11-25` or
`2025-06-18` and answers any other requested version with `2025-11-25`. Its
result includes `instructions` describing the search, open and find workflow.
An `MCP-Protocol-Version` header with another value returns HTTP 400 with the
REST error envelope instead of a JSON-RPC body, which lets newer clients fall
back to `initialize`. Send `notifications/initialized`, then call tools. DELETE
terminates the session. GET returns 405 because no unsolicited SSE stream is
provided. Cancelling a request cancels its work without affecting another
coalesced caller that is still waiting.

`tools/list` returns the tools in a fixed order with `name`, `title`,
`description`, `annotations` and `inputSchema`. Every tool is read-only.
`openWorldHint` is true for `web_search` and `web_open` and false for `web_find`
and `web_search_status`, which only read local state.

HTTP sessions expire after 30 idle minutes, with a maximum of 100 globally and
20 per caller identity. At capacity, only the requesting identity's oldest idle
session without active requests can be evicted. If none is eligible,
initialization returns 503. Request history is limited to 4,096 IDs and 1 MiB.
An expired or retired session returns 404 and the client reinitializes.

Stdio follows the child-process lifetime and retains a bounded recent ID window
plus all active IDs, without a lifetime request-count quota. Clients must still
use unique request IDs throughout a session. Blank lines are ignored, CRLF line
endings and a leading UTF-8 BOM are accepted, and a line longer than 1 MiB is
discarded and answered with Invalid Request and `id: null` while the process
keeps serving. A cancellation that arrives just before its request starts is
still honoured.
