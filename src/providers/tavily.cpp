#include "providers_internal.hpp"

#include <QDateTime>
#include <algorithm>

namespace qws {
namespace {
std::string date(const Json &item) {
  const auto value = resultText(item, "published_date");
  auto dateText = QString::fromStdString(value);
  if (dateText.endsWith(" GMT", Qt::CaseInsensitive))
    dateText.replace(dateText.size() - 3, 3, "+0000");
  const auto parsed = QDateTime::fromString(dateText, Qt::RFC2822Date);
  return parsed.isValid() ? parsed.toUTC().toString(Qt::ISODate).toStdString() : value;
}
} // namespace

SearchOutcome searchTavily(const SearchRequest &request, const ProviderConfig &config,
                           const ProviderTransport &send, const std::shared_ptr<CancelToken> &cancel) {
  const auto locale = searchLocale(request);
  Json arguments = {{"query", request.arguments["query"]},
                    {"max_results", std::min(20, request.arguments["limit"].get<int>())},
                    {"include_answer", false}, {"include_raw_content", false}, {"include_images", false},
                    {"include_published_date", true}};
  if (!request.arguments["domains"].empty())
    arguments["include_domains"] = request.arguments["domains"];
  if (request.arguments["time_range"] != "any")
    arguments["time_range"] = request.arguments["time_range"];
  if (locale.language.size() == 2)
    arguments["language"] = locale.tag().toLower().toStdString();

  HttpRequest outgoing;
  outgoing.url = "https://api.tavily.com/search";
  outgoing.method = HttpMethod::Post;
  outgoing.body = arguments.dump();
  outgoing.contentType = "application/json";
  outgoing.headers = {{"Accept", "application/json"}, {"Authorization", "Bearer " + config.key}};
  const auto data = readProviderResults(send(std::move(outgoing), cancel));
  Json results = Json::array();
  for (const auto &item : data)
    if (const auto url = resultText(item, "url"); !url.empty())
      results.push_back(resultItem("tavily", url, resultText(item, "title"), resultText(item, "content"), date(item)));
  return resultsPage("tavily", std::move(results));
}
} // namespace qws
