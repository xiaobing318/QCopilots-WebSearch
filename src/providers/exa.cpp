#include "providers_internal.hpp"

#include <QDateTime>

namespace qws {
namespace {
std::string startDate(const Json &arguments) {
  const auto interval = arguments["time_range"].get<std::string>();
  const auto now = QDateTime::currentDateTimeUtc();
  QDateTime start;
  if (interval == "day")
    start = now.addDays(-1);
  else if (interval == "week")
    start = now.addDays(-7);
  else if (interval == "month")
    start = now.addMonths(-1);
  else if (interval == "year")
    start = now.addYears(-1);
  return start.toString(Qt::ISODate).toStdString();
}

std::string highlight(const Json &item) {
  const auto values = item.find("highlights");
  if (values != item.end() && values->is_array())
    for (const auto &entry : *values)
      if (entry.is_string() && !entry.get_ref<const std::string &>().empty())
        return entry.get<std::string>();
  return {};
}
} // namespace

SearchOutcome searchExa(const SearchRequest &request, const ProviderConfig &config,
                        const ProviderTransport &send, const std::shared_ptr<CancelToken> &cancel) {
  Json arguments = {{"query", request.arguments["query"]}, {"numResults", request.arguments["limit"]},
                    {"contents", {{"highlights", true}}}};
  if (!request.arguments["domains"].empty())
    arguments["includeDomains"] = request.arguments["domains"];
  if (auto date = startDate(request.arguments); !date.empty())
    arguments["startPublishedDate"] = std::move(date);

  HttpRequest outgoing;
  outgoing.url = "https://api.exa.ai/search";
  outgoing.method = HttpMethod::Post;
  outgoing.body = arguments.dump();
  outgoing.contentType = "application/json";
  outgoing.headers = {{"Accept", "application/json"}, {"x-api-key", config.key}};
  const auto data = readProviderResults(send(std::move(outgoing), cancel));
  Json results = Json::array();
  for (const auto &item : data)
    if (const auto url = resultText(item, "url"); !url.empty())
      results.push_back(resultItem("exa", url, resultText(item, "title"), highlight(item),
                                   resultText(item, "publishedDate")));
  return resultsPage("exa", std::move(results));
}
} // namespace qws
