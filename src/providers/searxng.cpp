#include "searxng.hpp"
#include <QStringList>

namespace qws {
namespace {
class SearxngProvider final : public SearchProvider {
public:
  SearxngProvider(ProviderConfig config, const HttpClient &http, BackendSearchTransport transport)
      : config_(std::move(config)), http_(http), transport_(std::move(transport)) {}

  SearchOutcome search(const SearchRequest &request, const std::shared_ptr<CancelToken> &token) override {
    const auto &args = request.arguments;
    QString query = QString::fromStdString(args["query"].get<std::string>());
    QStringList hints, engines, form;
    for (const auto &domain : args["domains"])
      hints.push_back("site:" + QString::fromStdString(domain.get<std::string>()));
    if (!hints.empty())
      query += " (" + hints.join(" OR ") + ")";
    for (const auto &engine : config_.engines)
      engines.push_back(QString::fromStdString(engine));
    auto add = [&](const QString &key, const QString &value) {
      form.push_back(QString::fromLatin1(QUrl::toPercentEncoding(key)) + "=" +
                     QString::fromLatin1(QUrl::toPercentEncoding(value)));
    };
    add("q", query);
    add("format", "json");
    add("language", QString::fromStdString(args["language"].get<std::string>()));
    add("pageno", QString::number(request.page));
    if (args["time_range"] != "any")
      add("time_range", QString::fromStdString(args["time_range"].get<std::string>()));
    add("engines", engines.join(','));
    auto body = form.join('&').toStdString();
    const auto response = transport_ ? transport_(config_.url, std::move(body), config_.key, token)
                                     : http_.requestBackend(config_.url, std::move(body), config_.key,
                                                            token, config_.caFile);
    if (response.status == 429)
      throw Error("rate_limited", "Search provider is rate limited.", 429, true, retryAfterSeconds(response))
          .withReason("provider_rate_limited");
    if (response.status != 200)
      throw Error("provider_unavailable", "Search provider did not return a usable response.", 502,
                  response.status != 401 && response.status != 403)
          .withReason(response.status == 401 || response.status == 403 ? "provider_credentials_rejected" :
                      response.status >= 300 && response.status < 400 ? "provider_redirect_rejected" :
                      "provider_http_error");
    auto data = parseBoundedJson(response.body);
    if (data.is_discarded())
      throw Error("provider_unavailable", "Search provider returned invalid JSON results.", 502, true)
          .withReason("provider_invalid_json");
    if (!data.is_object() || !data.contains("results") || !data["results"].is_array())
      throw Error("provider_unavailable", "Search provider returned invalid JSON results.", 502, true)
          .withReason("provider_invalid_response");
    return {std::move(data), "searxng"};
  }
private:
  ProviderConfig config_;
  const HttpClient &http_;
  BackendSearchTransport transport_;
};
} // namespace
std::unique_ptr<SearchProvider> makeSearxng(const ProviderConfig &config, const HttpClient &http,
                                           const BackendSearchTransport &transport) {
  return std::make_unique<SearxngProvider>(config, http, transport);
}
} // namespace qws
