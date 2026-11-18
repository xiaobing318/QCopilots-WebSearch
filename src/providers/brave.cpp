#include "providers_internal.hpp"

#include <QUrlQuery>
#include <algorithm>

namespace qws {
namespace {
QString freshness(const SearchRequest &request) {
  const auto time = request.arguments["time_range"].get<std::string>();
  if (time == "day")
    return "pd";
  if (time == "week")
    return "pw";
  if (time == "month")
    return "pm";
  if (time == "year")
    return "py";
  return {};
}
} // namespace

SearchOutcome searchBrave(const SearchRequest &request, const ProviderConfig &config,
                          const ProviderTransport &send, const std::shared_ptr<CancelToken> &cancel) {
  const auto locale = searchLocale(request);
  QUrlQuery query;
  query.addQueryItem("q", queryWithDomains(request));
  query.addQueryItem("count", QString::number(std::min(20, request.arguments["limit"].get<int>())));
  query.addQueryItem("result_filter", "web");
  query.addQueryItem("text_decorations", "false");
  if (!locale.language.isEmpty())
    query.addQueryItem("search_lang", locale.language == "zh"
        ? (locale.region == "TW" || locale.region == "HK" ? "zh-hant" : "zh-hans") : locale.language);
  if (auto interval = freshness(request); !interval.isEmpty())
    query.addQueryItem("freshness", interval);
  HttpRequest outgoing;
  outgoing.url = "https://api.search.brave.com/res/v1/web/search?" + formEncode(query);
  outgoing.headers = {{"Accept", "application/json"}, {"X-Subscription-Token", config.key}};
  const auto data = readProviderResults(send(std::move(outgoing), cancel), "web");
  Json results = Json::array();
  for (const auto &item : data)
    if (const auto url = resultText(item, "url"); !url.empty())
      results.push_back(resultItem("brave", url, resultText(item, "title"),
                                   resultText(item, "description"), resultText(item, "page_age")));
  return resultsPage("brave", std::move(results));
}
} // namespace qws
