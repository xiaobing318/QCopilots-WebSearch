// Source order and pacing are based on Strata's MIT-licensed web search
// integration. See packaging/licenses/strata/LICENSE.
#include "providers_internal.hpp"

#include <algorithm>
#include <optional>

namespace qws {
using namespace std::chrono_literals;
namespace {
bool denied(int status) {
  return status == 202 || status == 403 || status == 429;
}

bool reportableFailure(const Error &error) {
  return error.code == "rate_limited" || error.code == "provider_unavailable" || error.code == "timeout";
}

class AutoProvider final : public SearchProvider {
public:
  AutoProvider(std::shared_ptr<AutoState> state, ProviderTransport send)
      : state_(std::move(state)), send_(std::move(send)) {}

  SearchOutcome search(const SearchRequest &request, const std::shared_ptr<CancelToken> &token) override {
    checkSearchCancellation(token);
    // Share the caller's remaining time with the fallback instead of spending
    // the entire deadline on repeated attempts against DuckDuckGo.
    const auto now = Clock::now();
    const auto duckDeadline = now + (token->deadline - now) * 2 / 3;
    int duckRetry = 0;
    std::optional<Error> lastFailure;
    try {
      return duckDuckGo(request, token, duckDeadline);
    } catch (const Error &error) {
      if (!reportableFailure(error))
        throw;
      lastFailure.emplace(error);
      if (error.code == "rate_limited")
        duckRetry = error.retryAfterSeconds;
    }

    checkSearchCancellation(token);
    try {
      auto result = page(bingRequest(request), SerpSource::Bing, state_->bing, token, token->deadline);
      result.warnings.push_back("DuckDuckGo was busy, results come from Bing.");
      result.timeRangeWarning = "Bing did not apply the requested time range.";
      return result;
    } catch (const Error &error) {
      if (!reportableFailure(error))
        throw;
      lastFailure.emplace(error);
      if (duckRetry > 0 && error.code == "rate_limited")
        throw Error("rate_limited", "All search sources are rate limited.", 429, true,
                    std::min(duckRetry, error.retryAfterSeconds))
            .withReason("provider_rate_limited");
    }
    checkSearchCancellation(token);
    throw Error("provider_unavailable", "No search source returned a usable response.", 502,
                lastFailure ? lastFailure->retryable : true,
                lastFailure ? lastFailure->retryAfterSeconds : 0)
        .withReason(lastFailure && !lastFailure->reason.empty() ? lastFailure->reason :
                    "provider_sources_unavailable");
  }

private:
  std::shared_ptr<AutoState> state_;
  ProviderTransport send_;

  SearchOutcome duckDuckGo(const SearchRequest &request, const std::shared_ptr<CancelToken> &token,
                           Clock::time_point deadline) {
    try {
      auto result = page(duckDuckGoRequest(request, SerpSource::DuckDuckGoHtml),
                         SerpSource::DuckDuckGoHtml, state_->duckDuckGo, token, deadline);
      if (!result.page["results"].empty())
        return result;
    } catch (const Error &error) {
      // An explicit refusal applies to DuckDuckGo itself, not just the HTML
      // form. Do not issue another request to the Lite endpoint in that case.
      if (error.code != "provider_unavailable")
        throw;
    }
    return page(duckDuckGoRequest(request, SerpSource::DuckDuckGoLite),
                SerpSource::DuckDuckGoLite, state_->duckDuckGo, token, deadline);
  }

  SearchOutcome page(HttpRequest request, SerpSource source, SearchPacer &pacer,
                     const std::shared_ptr<CancelToken> &token, Clock::time_point deadline) {
    std::optional<Error> lastFailure;
    for (int attempt = 0; attempt < 2; ++attempt) {
      const auto lease = pacer.acquire(token, deadline);
      const auto remaining = std::chrono::ceil<std::chrono::milliseconds>(deadline - Clock::now());
      if (remaining <= 0ms)
        throw Error("timeout", "Search source deadline expired.", 504, true).withReason("request_timeout");
      request.timeout = std::min(remaining, 2500ms);

      HttpResponse response;
      try {
        response = send_(request, token);
      } catch (const Error &error) {
        if (error.code == "cancelled" || error.code == "internal_error" || error.code == "invalid_argument")
          throw;
        lastFailure.emplace(error);
        if (error.code == "timeout" && attempt == 0)
          continue;
        break;
      }
      if (response.status >= 500 && response.status < 600 && attempt == 0)
        continue;
      if (denied(response.status))
        throw Error("rate_limited", "Search source refused the request.", 429, true,
                    pacer.refused(retryAfterSeconds(response)))
            .withReason("provider_rate_limited");
      if (response.status != 200) {
        lastFailure.emplace(Error("provider_unavailable", "Search source returned an HTTP error.", 502,
                                  response.status == 408 || response.status >= 500)
                                .withReason("provider_http_error"));
        break;
      }

      const auto &url = response.url.empty() ? request.url : response.url;
      auto result = parseSerp(source, response.body, url);
      if (result.blocked)
        throw Error("rate_limited", "Search source refused the request.", 429, true,
                    pacer.refused(retryAfterSeconds(response)))
            .withReason("provider_rate_limited");
      if (!result.recognized) {
        lastFailure.emplace(Error("provider_unavailable", "Search source returned an unrecognized results page.",
                                  502, true).withReason("provider_invalid_response"));
        break;
      }
      pacer.accepted();
      return resultsPage(source == SerpSource::Bing ? "bing" : "duckduckgo", std::move(result.results));
    }
    checkSearchCancellation(token);
    throw Error("provider_unavailable", "Search source did not return a usable results page.", 502,
                lastFailure ? lastFailure->retryable : true,
                lastFailure ? lastFailure->retryAfterSeconds : 0)
        .withReason(lastFailure && !lastFailure->reason.empty() ? lastFailure->reason :
                    "provider_sources_unavailable");
  }
};
} // namespace

Json AutoState::status() const {
  return {{"duckduckgo", duckDuckGo.status()}, {"bing", bing.status()}};
}

std::unique_ptr<SearchProvider> makeAuto(std::shared_ptr<AutoState> state, ProviderTransport send) {
  return std::make_unique<AutoProvider>(std::move(state), std::move(send));
}
} // namespace qws
