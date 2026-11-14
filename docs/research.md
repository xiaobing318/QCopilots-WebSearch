# Feasibility and adoption record

Research baseline: 2026-11-14. The project implements a reusable search/read/find
tool experience with a native C++ service and a replaceable search provider. It
does not reproduce a commercial vendor's global index or private search backend.
Self-hosting removes this software's commercial API quota. Upstream providers
still determine availability, coverage, throttling and CAPTCHA behavior.

| Project or data source | Fit and decision |
| --- | --- |
| [SearXNG](https://docs.searxng.org/dev/search_api.html) | Adopt its self-hosted JSON API as the first search backend |
| [4get](https://git.lolcat.ca/lolcat/4get) | Similar self-hosted metasearch, alternative provider candidate |
| [open-webSearch](https://github.com/Aas-ee/open-webSearch) | Similar MCP tools, useful interoperability reference, different runtime stack |
| [mcp-searxng](https://github.com/ihor-sokoliuk/mcp-searxng) | Demonstrates SearXNG-to-MCP integration, not reused as the native server |
| [Vane](https://github.com/ItzCrazyKns/Vane) | Search plus LLM application, its UI/model orchestration is outside scope |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | Broad crawling/extraction capabilities with a heavier deployment stack |
| [Firecrawl](https://github.com/firecrawl/firecrawl) | Crawling/extraction service, unnecessary for the initial bounded HTML/text reader |
| [YaCy](https://github.com/yacy/yacy_search_server) | Independent crawling/indexing, requires a separate coverage and operations investment |
| [Mwmbl](https://github.com/mwmbl/mwmbl) | Independent search-index approach, not a drop-in replacement for web-scale coverage |
| [Common Crawl](https://commoncrawl.org/overview) | Useful crawl data, not a turnkey online search service |
| [OpenWebSearch.eu](https://openwebsearch.eu/open-webindex/) | Open index resources, possible future research input |
| [Whoogle](https://github.com/benbusby/whoogle-search) | Archived at the research baseline, not selected as a core dependency |
| [Stract](https://github.com/StractOrg/stract) | Archived at the research baseline, maintenance continuity insufficient for adoption |

The public [DeepSeek Harness web module](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/README.md)
also separates the tool experience from its provider implementation. This supports
the architectural choice of a replaceable provider. It does not establish access
to Codex, Claude or DeepSeek's privately operated search infrastructure.

The initial scope is search, static HTML/text reading and literal snapshot search.
It excludes autonomous indexing, paid providers, JavaScript execution, PDFs and
LLM answers. The native implementation uses Qt Core/Network/SQL, cpp-httplib,
libcurl and Lexbor. Qt HTTP Server is excluded because its
[module licensing](https://doc.qt.io/qt-6/qthttpserver-index.html) does not match
the intended Apache-2.0 project route.

The QGIS adapter is intentionally separate and keeps the QGIS GPL license.
REST and MCP clients in other projects can use the native service without QGIS.
SearXNG remains an independently deployed AGPL service.

Public validation reports must separate full results, partial results, successful
empty searches, provider errors, cache hits and transport failures. There is no
fixed public-query success-rate launch threshold. Deterministic service errors,
security failures and packaging defects cannot be excused as public-web variability.
