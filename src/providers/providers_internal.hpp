#pragma once

#include "pacing.hpp"
#include "serp_parsers.hpp"
#include <qws/providers.hpp>

namespace qws {
using ProviderTransport = std::function<HttpResponse(HttpRequest, const std::shared_ptr<CancelToken> &)>;

struct SearchLocale {
  QString language;
  QString region;
  QString tag() const;
};

SearchLocale searchLocale(const SearchRequest &);
QString queryWithDomains(const SearchRequest &);
std::string formEncode(const QUrlQuery &);
std::string resultText(const Json &object, const char *field);
Json readProviderResults(const HttpResponse &, const char *container = nullptr);
Json resultItem(std::string_view source, std::string url, std::string title,
                std::string summary, std::string published = {});
SearchOutcome resultsPage(std::string source, Json results);
void checkSearchCancellation(const std::shared_ptr<CancelToken> &);

HttpRequest duckDuckGoRequest(const SearchRequest &, SerpSource);
HttpRequest bingRequest(const SearchRequest &);
SearchOutcome searchBrave(const SearchRequest &, const ProviderConfig &, const ProviderTransport &,
                          const std::shared_ptr<CancelToken> &);
SearchOutcome searchTavily(const SearchRequest &, const ProviderConfig &, const ProviderTransport &,
                           const std::shared_ptr<CancelToken> &);
SearchOutcome searchExa(const SearchRequest &, const ProviderConfig &, const ProviderTransport &,
                        const std::shared_ptr<CancelToken> &);

struct AutoState {
  explicit AutoState(std::chrono::milliseconds interval = std::chrono::milliseconds(2500),
                     std::chrono::milliseconds jitter = std::chrono::milliseconds(1500))
      : duckDuckGo(interval, jitter), bing(interval, jitter) {}
  SearchPacer duckDuckGo, bing;
  Json status() const;
};
std::unique_ptr<SearchProvider> makeAuto(std::shared_ptr<AutoState>, ProviderTransport);
} // namespace qws
