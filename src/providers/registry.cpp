#include "providers_internal.hpp"
#include "searxng.hpp"

namespace qws {
namespace {
using KeyedSearch = decltype(&searchBrave);

class KeyedProvider final : public SearchProvider {
public:
  KeyedProvider(ProviderConfig config, ProviderTransport send, KeyedSearch search)
      : config_(std::move(config)), send_(std::move(send)), search_(search) {}

  SearchOutcome search(const SearchRequest &request, const std::shared_ptr<CancelToken> &token) override {
    if (!config_.ready())
      throw Error("provider_not_configured", "The search provider needs a key.", 503);
    checkSearchCancellation(token);
    try {
      return search_(request, config_, send_, token);
    } catch (const Error &error) {
      // The transport may include page-oriented error codes. Keyed providers
      // report endpoint failures without copying upstream messages or bodies.
      if (error.code == "upstream_http_error" || error.code == "destination_blocked" ||
          error.code == "response_too_large" || error.code == "unsupported_content")
        throw Error("provider_unavailable", "Search provider request failed.", 502, error.retryable);
      throw;
    }
  }

private:
  ProviderConfig config_;
  ProviderTransport send_;
  KeyedSearch search_;
};
} // namespace

struct ProviderRegistry::Impl {
  std::shared_ptr<AutoState> autoState = std::make_shared<AutoState>();
};
ProviderRegistry::ProviderRegistry() : impl_(std::make_unique<Impl>()) {}
ProviderRegistry::~ProviderRegistry() = default;

std::unique_ptr<SearchProvider> ProviderRegistry::create(const ProviderConfig &config, const HttpClient &http,
                                                          const BackendSearchTransport &transport) const {
  if (config.id == "searxng")
    return makeSearxng(config, http, transport);
  ProviderTransport send = [&http](HttpRequest request, const std::shared_ptr<CancelToken> &cancel) {
    return http.send(std::move(request), cancel);
  };
  if (config.id == "auto")
    return makeAuto(impl_->autoState, std::move(send));
  if (config.id == "brave")
    return std::make_unique<KeyedProvider>(config, std::move(send), searchBrave);
  if (config.id == "tavily")
    return std::make_unique<KeyedProvider>(config, std::move(send), searchTavily);
  if (config.id == "exa")
    return std::make_unique<KeyedProvider>(config, std::move(send), searchExa);
  if (config.id == "perplexity")
    return std::make_unique<KeyedProvider>(config, std::move(send), searchPerplexity);
  if (config.id == "deepseek")
    return std::make_unique<KeyedProvider>(config, std::move(send), searchDeepSeek);
  throw Error("invalid_argument", "Unknown search provider.");
}

Json ProviderRegistry::status() const {
  return impl_->autoState->status();
}

void checkSearchCancellation(const std::shared_ptr<CancelToken> &token) {
  if (!token)
    throw Error("invalid_argument", "A search deadline is required.");
  if (token->cancelled.load())
    throw Error("cancelled", "Search was cancelled.", 499);
  if (Clock::now() >= token->deadline)
    throw Error("timeout", "Search deadline expired.", 504, true);
}

std::string resultText(const Json &object, const char *field) {
  const auto it = object.find(field);
  return it != object.end() && it->is_string() ? it->get<std::string>() : std::string();
}

Json readProviderResults(const HttpResponse &response, const char *container) {
  if (response.status == 429)
    throw Error("rate_limited", "Search provider is rate limited.", 429, true, retryAfterSeconds(response));
  if (response.status != 200) {
    const auto status = response.status;
    throw Error("provider_unavailable", "Search provider did not return a usable response.", 502,
                status == 408 || status >= 500);
  }
  auto data = parseBoundedJson(response.body);
  if (data.is_object()) {
    auto items = &data;
    if (container) {
      const auto nested = data.find(container);
      if ((nested == data.end() || nested->is_null()) && resultText(data, "type") == "search")
        return Json::array();
      if (nested == data.end() || !nested->is_object())
        throw Error("provider_unavailable", "Search provider returned invalid JSON results.", 502, true);
      items = &*nested;
    }
    if (const auto results = items->find("results"); results != items->end() && results->is_array())
      return std::move(*results);
  }
  throw Error("provider_unavailable", "Search provider returned invalid JSON results.", 502, true);
}

Json resultItem(std::string_view source, std::string url, std::string title, std::string summary,
                std::string published) {
  auto item = Json{{"url", std::move(url)}, {"title", std::move(title)},
                   {"content", std::move(summary)}, {"engines", Json::array({source})}};
  if (!published.empty())
    item["publishedDate"] = std::move(published);
  return item;
}

SearchOutcome resultsPage(std::string source, Json results) {
  return {Json{{"results", std::move(results)}}, std::move(source)};
}
} // namespace qws
