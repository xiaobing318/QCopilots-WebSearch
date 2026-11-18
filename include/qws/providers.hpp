#pragma once
#include <functional>
#include <qws/network.hpp>
#include <qws/settings.hpp>

namespace qws {
using BackendSearchTransport =
    std::function<HttpResponse(std::string, std::string, std::string, std::shared_ptr<CancelToken>)>;

struct SearchRequest {
  Json arguments;
  int page = 1;
};

struct SearchOutcome {
  // Provider-normalized SearXNG-shaped page consumed by shared result processing.
  Json page;
  std::string source;
  Json warnings = Json::array();
};

class SearchProvider {
public:
  virtual ~SearchProvider() = default;
  virtual SearchOutcome search(const SearchRequest &, const std::shared_ptr<CancelToken> &) = 0;
};

class ProviderRegistry {
public:
  ProviderRegistry();
  ~ProviderRegistry();
  std::unique_ptr<SearchProvider> create(const ProviderConfig &, const HttpClient &,
                                         const BackendSearchTransport & = {}) const;
  Json status() const;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
} // namespace qws
