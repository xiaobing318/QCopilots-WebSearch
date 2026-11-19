#include "providers_internal.hpp"

#include <QDateTime>

namespace qws {
namespace {
constexpr qsizetype QueryLimit = 128;

QDate firstDate(const Json &arguments) {
  const auto today = QDateTime::currentDateTimeUtc().date();
  const auto &interval = arguments["time_range"];
  if (interval == "day")
    return today.addDays(-1);
  if (interval == "week")
    return today.addDays(-7);
  if (interval == "month")
    return today.addMonths(-1);
  if (interval == "year")
    return today.addYears(-1);
  return {};
}

void appendHint(QString &query, const QString &hint) {
  if (query.toUcs4().size() + 1 + hint.toUcs4().size() <= QueryLimit)
    query += ' ' + hint;
}

QString deepSeekQuery(const SearchRequest &request) {
  auto query = QString::fromStdString(request.arguments["query"].get<std::string>());
  if (query.toUcs4().size() > QueryLimit)
    throw Error("invalid_argument", "DeepSeek Search accepts at most 128 Unicode characters in a query.");

  QStringList sites;
  for (const auto &domain : request.arguments["domains"])
    sites.push_back("site:" + QString::fromStdString(domain.get<std::string>()));
  // Only send the complete domain expression. The service always filters results locally.
  if (!sites.isEmpty())
    appendHint(query, sites.join(" OR "));
  const auto locale = searchLocale(request);
  if (locale.language.size() == 2)
    appendHint(query, "lang:" + locale.language);
  if (const auto date = firstDate(request.arguments); date.isValid())
    appendHint(query, "after:" + date.toString(Qt::ISODate));
  return query;
}
} // namespace

SearchOutcome searchDeepSeek(const SearchRequest &request, const ProviderConfig &config,
                             const ProviderTransport &send, const std::shared_ptr<CancelToken> &cancel) {
  const Json arguments = {{"query", deepSeekQuery(request).toStdString()}, {"count", request.arguments["limit"]},
                          {"need_content", false}};
  HttpRequest outgoing;
  outgoing.url = "https://api.deepseek.com/v1/tools/search";
  outgoing.method = HttpMethod::Post;
  outgoing.body = arguments.dump();
  outgoing.contentType = "application/json";
  outgoing.headers = {{"Accept", "application/json"}, {"Authorization", "Bearer " + config.key}};
  const auto data = readProviderResults(send(std::move(outgoing), cancel));
  Json results = Json::array();
  for (const auto &item : data)
    if (const auto url = resultText(item, "url"); !url.empty()) {
      auto snippet = resultText(item, "snippet");
      if (snippet.empty())
        snippet = resultText(item, "summary");
      results.push_back(resultItem("deepseek", url, resultText(item, "title"), std::move(snippet),
                                   resultText(item, "date")));
    }
  return resultsPage("deepseek", std::move(results));
}
} // namespace qws
