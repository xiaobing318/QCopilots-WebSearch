#include "providers_internal.hpp"

namespace qws {
SearchOutcome searchPerplexity(const SearchRequest &request, const ProviderConfig &config,
                               const ProviderTransport &send, const std::shared_ptr<CancelToken> &cancel) {
  const auto locale = searchLocale(request);
  Json arguments = {{"query", request.arguments["query"]}, {"max_results", request.arguments["limit"]}};
  if (!request.arguments["domains"].empty())
    arguments["search_domain_filter"] = request.arguments["domains"];
  if (request.arguments["time_range"] != "any")
    arguments["search_recency_filter"] = request.arguments["time_range"];
  if (locale.language.size() == 2)
    arguments["search_language_filter"] = Json::array({locale.language.toStdString()});

  HttpRequest outgoing;
  outgoing.url = "https://api.perplexity.ai/search";
  outgoing.method = HttpMethod::Post;
  outgoing.body = arguments.dump();
  outgoing.contentType = "application/json";
  outgoing.headers = {{"Accept", "application/json"}, {"Authorization", "Bearer " + config.key}};
  const auto data = readProviderResults(send(std::move(outgoing), cancel));
  Json results = Json::array();
  for (const auto &item : data)
    if (const auto url = resultText(item, "url"); !url.empty())
      results.push_back(resultItem("perplexity", url, resultText(item, "title"),
                                   resultText(item, "snippet"), resultText(item, "date")));
  return resultsPage("perplexity", std::move(results));
}
} // namespace qws
