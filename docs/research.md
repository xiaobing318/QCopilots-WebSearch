<a name="feasibility-and-adoption-record"></a>

# 可行性调研与选型记录

本页以 2026 年 11 月 14 日的调研为基准。项目使用原生 C++ 服务和可替换的搜索提供方，提供可复用的搜索、网页阅读和页内查找工具。商业厂商的全球索引和私有搜索后端超出复现范围。自托管免去了本软件这一层的商业 API 配额。服务可用性、搜索覆盖范围、限流和验证码仍由上游提供方决定。

| 项目或数据源 | 适用性与选型结论 |
| --- | --- |
| [SearXNG](https://docs.searxng.org/dev/search_api.html) | 采用其自托管 JSON API 作为首个搜索后端 |
| [4get](https://git.lolcat.ca/lolcat/4get) | 同类自托管元搜索工具，可作为备选提供方 |
| [open-webSearch](https://github.com/Aas-ee/open-webSearch) | 提供同类 MCP 工具，适合作为互操作性参考，使用不同的运行时技术栈 |
| [mcp-searxng](https://github.com/ihor-sokoliuk/mcp-searxng) | 作为 SearXNG 对接 MCP 的参考，原生服务由本项目实现 |
| [Vane](https://github.com/ItzCrazyKns/Vane) | 将搜索与大语言模型结合的应用，其界面和模型编排超出本项目范围 |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | 爬取和内容提取功能较全面，所需部署组件较多 |
| [Firecrawl](https://github.com/firecrawl/firecrawl) | 提供爬取和提取服务，初期限定范围的 HTML 和文本阅读功能无需引入 |
| [YaCy](https://github.com/yacy/yacy_search_server) | 提供独立的爬取和索引能力，需要单独投入资源保障覆盖和运维 |
| [Mwmbl](https://github.com/mwmbl/mwmbl) | 采用独立搜索索引，要达到全网覆盖需要另行评估 |
| [Common Crawl](https://commoncrawl.org/overview) | 提供可用的爬取数据，需基于数据另行构建在线搜索服务 |
| [OpenWebSearch.eu](https://openwebsearch.eu/open-webindex/) | 提供开放索引资源，可用于后续研究 |
| [Whoogle](https://github.com/benbusby/whoogle-search) | 调研时已归档，未选为核心依赖 |
| [Stract](https://github.com/StractOrg/stract) | 调研时已归档，持续维护条件不足以支持采用 |

公开的 DeepSeek Harness Web 模块也将工具接口与提供方实现分离，为采用可替换提供方的设计提供了参考。访问 Codex、Claude 或 DeepSeek 私有搜索基础设施的权限需要另行确认。 [DeepSeek Harness Web 模块](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/README.md)

初期范围包括搜索、静态 HTML 和文本阅读，以及对网页快照进行字面查找。自主建索引、付费提供方、JavaScript 执行、PDF 和大语言模型生成的答案均超出初期范围。原生实现使用 Qt Core、Qt Network、Qt SQL、cpp-httplib、libcurl 和 Lexbor。Qt HTTP Server 的模块许可与项目采用 Apache-2.0 的方案不符，因此未引入。 [Qt HTTP Server 模块许可](https://doc.qt.io/qt-6/qthttpserver-index.html)

QGIS 适配器作为独立组件，沿用 QGIS 的 GPL 许可。其他项目的 REST 和 MCP 客户端可以直接使用原生服务。SearXNG 仍是独立部署的 AGPL 服务。

公开验收报告须分别记录完整结果、部分结果、查询成功但结果为空、提供方错误、缓存命中和传输失败。上线评估没有固定的公开查询成功率门槛。可稳定复现的服务错误、安全故障和打包缺陷均须独立处理。
