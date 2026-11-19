#include "test_support.hpp"
#include "../../src/providers/providers_internal.hpp"

#include <QCoreApplication>
#include <QUrlQuery>
#include <fstream>
#include <iostream>

using namespace qws;
using namespace std::chrono_literals;

namespace {
std::string fixture(const std::string &directory, const char *name) {
  std::ifstream file(directory + "/" + name, std::ios::binary);
  require(bool(file), "Missing fixture.");
  return std::string(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
}

SearchRequest query() {
  return {Json{{"query", "C++ & QGIS"}, {"limit", 4}, {"language", "zh-CN"},
               {"time_range", "week"}, {"domains", Json::array({"example.com"})}}};
}

HttpResponse jsonResponse(Json body) {
  return {200, body.dump(), "application/json", {}, {}};
}

void transportValidation() {
  HttpResponse response;
  response.headers["retry-after"] = "120";
  require(retryAfterSeconds(response) == 120, "Retry-After delta was lost.");
  response.headers["retry-after"] = "999999999999999999";
  require(retryAfterSeconds(response) == 600, "Large Retry-After delta must be bounded.");
  response.headers["retry-after"] = "Fri, 31 Dec 2099 23:59:59 GMT";
  require(retryAfterSeconds(response) == 600, "HTTP-date Retry-After was ignored.");
  response.headers["retry-after"] = "unknown";
  require(retryAfterSeconds(response) == 0, "Malformed Retry-After was accepted.");

  HttpClient client;
  HttpRequest outgoing;
  outgoing.url = "https://api.example.com/";
  auto deadline = std::make_shared<CancelToken>();
  outgoing.headers["Host"] = "another.example.com";
  requireError("invalid_argument", [&] { client.send(outgoing, deadline); });
  outgoing.headers = {{"Accept", "invalid\r\nX-Test: added"}};
  requireError("invalid_argument", [&] { client.send(outgoing, deadline); });
  outgoing.headers = {{"Authorization", "Bearer a-test-key"}};
  outgoing.redirectHosts = {"another.example.com"};
  requireError("invalid_argument", [&] { client.send(outgoing, deadline); });
  outgoing.headers.clear();
  outgoing.redirectHosts.clear();
  outgoing.url = "http://api.example.com/";
  requireError("invalid_argument", [&] { client.send(outgoing, deadline); });
  outgoing.url = "https://127.0.0.1/";
  requireError("destination_blocked", [&] { client.send(outgoing, deadline); });
}

void serpFixtures(const std::string &directory) {
  auto ddg = parseSerp(SerpSource::DuckDuckGoHtml, fixture(directory, "ddg.html"),
                       "https://html.duckduckgo.com/html/");
  require(ddg.recognized && !ddg.blocked && ddg.results.size() == 2, "Incorrect DuckDuckGo results.");
  require(ddg.results[0]["url"] == "https://example.com/qgis?x=1&y=two%20words", "DDG wrapper was not decoded.");
  require(ddg.results[0]["title"] == "Open & GIS", "DDG entities were not decoded.");
  auto lite = parseSerp(SerpSource::DuckDuckGoLite, fixture(directory, "ddg-lite.html"),
                        "https://lite.duckduckgo.com/lite/");
  require(lite.recognized && lite.results[0]["content"] == "A separate table row.", "Missing Lite snippet.");
  auto bing = parseSerp(SerpSource::Bing, fixture(directory, "bing.html"), "https://www.bing.com/search");
  require(bing.recognized && bing.results[0]["url"] == "https://example.net/bing?ref=search",
          "Bing wrapper was not decoded.");
  auto unknown = parseSerp(SerpSource::Bing, fixture(directory, "unrecognized.html"), "https://www.bing.com/search");
  require(!unknown.recognized && !unknown.blocked && unknown.results.empty(), "Undetected markup change.");
}

void autoFallback(const std::string &directory) {
  auto token = std::make_shared<CancelToken>();
  auto state = std::make_shared<AutoState>(0ms, 0ms);
  std::vector<std::string> visited;
  const auto html = fixture(directory, "ddg-empty.html");
  const auto lite = fixture(directory, "ddg-lite.html");
  const auto bing = fixture(directory, "bing.html");
  auto autoProvider = makeAuto(state, [&](HttpRequest outgoing, const auto &received) {
    require(token == received && outgoing.timeout > 0ms && outgoing.timeout <= 2500ms,
            "Automatic attempts must share the caller's token and use a bounded timeout.");
    visited.push_back(outgoing.url);
    require(outgoing.method == HttpMethod::Post, "Unexpected automatic request method.");
    require(outgoing.body.find("q=C%2B%2B%20%26%20QGIS") == 0, "DDG query must be percent-encoded.");
    require(outgoing.body.find("kl=cn-zh") != std::string::npos, "DDG region was omitted.");
    require(outgoing.body.find("df=w") != std::string::npos, "DDG date preference was omitted.");
    if (visited.size() == 1)
      return HttpResponse{503};
    return HttpResponse{200, visited.size() == 2 ? html : lite};
  });
  auto result = autoProvider->search(query(), token);
  require(visited == std::vector<std::string>{"https://html.duckduckgo.com/html/",
                                               "https://html.duckduckgo.com/html/",
                                               "https://lite.duckduckgo.com/lite/"},
          "Expected one retry before trying DuckDuckGo Lite.");
  require(result.source == "duckduckgo" && result.page["results"].size() == 1 && result.warnings.empty(),
          "Lite must preserve the actual source.");

  visited.clear();
  autoProvider = makeAuto(state, [&](HttpRequest outgoing, const auto &) {
    visited.push_back(outgoing.url);
    if (outgoing.url.starts_with("https://html.duckduckgo.com"))
      return HttpResponse{200, fixture(directory, "ddg-blocked.html"), {}, {}, {{"retry-after", "100"}}};
    require(outgoing.url.starts_with("https://www.bing.com/search?"), "DuckDuckGo challenges must skip Lite.");
    require(outgoing.redirectHosts == std::vector<std::string>{"www.bing.com", "cn.bing.com"},
            "Bing redirect policy changed.");
    return HttpResponse{200, bing};
  });
  result = autoProvider->search(query(), token);
  require(result.source == "bing" && result.warnings == Json::array({"DuckDuckGo was busy, results come from Bing."}), "Bing fallback must be visible.");
  require(visited.size() == 2 && state->status()["duckduckgo"]["retry_after_seconds"] == 100,
          "DDG challenge did not set shared cooldown.");
  visited.clear();
  result = autoProvider->search(query(), token);
  require(result.source == "bing" && visited.size() == 1, "A cooling-down source must not receive traffic.");

  autoProvider = makeAuto(state, [](HttpRequest outgoing, const auto &) {
    require(outgoing.url.starts_with("https://www.bing.com/"), "DuckDuckGo should remain in cooldown.");
    return HttpResponse{429, {}, {}, {}, {{"retry-after", "60"}}};
  });
  try {
    autoProvider->search(query(), token);
    require(false, "Both sources were rate limited.");
  } catch (const Error &error) {
    require(error.code == "rate_limited" && error.retryAfterSeconds == 60, "Earliest retry time should be reported.");
  }

  state = std::make_shared<AutoState>(0ms, 0ms);
  int attempts = 0;
  autoProvider = makeAuto(state, [&attempts](auto, const auto &) {
    ++attempts;
    throw Error("timeout", "Fixture timeout.", 504, true);
    return HttpResponse{};
  });
  requireError("provider_unavailable", [&] { autoProvider->search(query(), token); });
  require(attempts == 6, "Unavailable endpoints each get a single retry.");
  token->cancelled = true;
  requireError("cancelled", [&] { autoProvider->search(query(), token); });
  require(attempts == 6, "Cancelled requests cannot reach providers.");
}

void pacingLimits() {
  auto token = std::make_shared<CancelToken>();
  SearchPacer quota(0ms, 0ms);
  for (int index = 0; index < 8; ++index)
    (void)quota.acquire(token);
  requireError("rate_limited", [&] { (void)quota.acquire(token); });
  require(quota.status()["requests_last_minute"] == 8, "One-minute quota is incorrect.");

  SearchPacer pacer(2500ms, 0ms);
  (void)pacer.acquire(token);
  requireError("timeout", [&] { (void)pacer.acquire(token, Clock::now() + 20ms); });
  require(pacer.refused(0) == 60 && pacer.refused(0) == 120 && pacer.refused(500) == 500,
          "Refusal backoff must grow, respecting a longer Retry-After.");
  require(pacer.refused(600) == 600 && pacer.refused(900) == 600, "Refusal backoff must be capped.");
  pacer.accepted();
  require(pacer.status()["consecutive_refusals"] == 0 && pacer.status()["retry_after_seconds"] == 0,
          "Successful searches must clear cooldown.");
}

void keyedSearch() {
  auto token = std::make_shared<CancelToken>();
  const std::string key = "example-fake-key";
  ProviderConfig config{"brave", {}, {}, key, {}};
  const Json items = Json::array({{{"url", "https://example.com/research"}, {"title", "A result"},
                                   {"description", "Brave summary"}, {"page_age", "2026-11-14T00:00:00Z"},
                                   {"content", "Tavily summary"}, {"published_date", "Tue, 11 Mar 2025 17:00:00 GMT"},
                                   {"highlights", {"Exa passage"}}, {"publishedDate", "2025-05-12"}}});
  auto brave = searchBrave(query(), config, [&](HttpRequest outgoing, const auto &) {
    const QUrl url(QString::fromStdString(outgoing.url));
    const QUrlQuery args(url);
    require(url.host() == "api.search.brave.com" && url.path() == "/res/v1/web/search", "Incorrect Brave endpoint.");
    require(args.queryItemValue("q", QUrl::FullyDecoded) == "C++ & QGIS (site:example.com)", "Incorrect Brave query.");
    require(args.queryItemValue("freshness") == "pw" && args.queryItemValue("search_lang") == "zh-hans",
            "Brave preferences were omitted.");
    require(outgoing.headers.at("X-Subscription-Token") == key && outgoing.redirectHosts.empty(), "Incorrect Brave authentication.");
    return jsonResponse({{"web", {{"results", items}}}});
  }, token);
  require(brave.source == "brave" && brave.page["results"][0]["content"] == "Brave summary",
          "Incorrect Brave result.");

  auto tavily = searchTavily(query(), config, [&](HttpRequest outgoing, const auto &) {
    const auto arguments = parseBoundedJson(outgoing.body);
    require(outgoing.url == "https://api.tavily.com/search" && outgoing.method == HttpMethod::Post,
            "Incorrect Tavily endpoint.");
    require(outgoing.headers.at("Authorization") == "Bearer " + key, "Incorrect Tavily authentication.");
    require(arguments["include_answer"] == false && arguments["time_range"] == "week" &&
                arguments["include_domains"] == Json::array({"example.com"}), "Tavily options changed.");
    return jsonResponse({{"results", items}});
  }, token);
  require(tavily.page["results"][0]["publishedDate"] == "2025-03-11T17:00:00Z", "Tavily timestamp is incorrect.");

  auto exa = searchExa(query(), config, [&](HttpRequest outgoing, const auto &) {
    const auto arguments = parseBoundedJson(outgoing.body);
    require(outgoing.url == "https://api.exa.ai/search" && outgoing.method == HttpMethod::Post,
            "Incorrect Exa endpoint.");
    require(outgoing.headers.at("x-api-key") == key, "Incorrect Exa authentication.");
    require(arguments["contents"]["highlights"] == true && arguments.contains("startPublishedDate") &&
                arguments["includeDomains"] == Json::array({"example.com"}), "Exa options changed.");
    return jsonResponse({{"results", items}});
  }, token);
  require(exa.page["results"][0]["content"] == "Exa passage", "Exa highlights should be result summaries.");

  for (const int status : {401, 403, 429, 500}) {
    try {
      (void)readProviderResults(HttpResponse{status, key, {}, {}, {{"retry-after", "90"}}});
      require(false, "Upstream error was treated as success.");
    } catch (const Error &error) {
      require(std::string(error.what()).find(key) == std::string::npos, "Upstream body leaked.");
      if (status == 429)
        require(error.code == "rate_limited" && error.retryAfterSeconds == 90, "Rate limit hint was discarded.");
      else
        require(error.code == "provider_unavailable" && error.retryable == (status == 500),
                "Incorrect upstream error mapping.");
    }
  }
  requireError("provider_unavailable", [&] { (void)readProviderResults(HttpResponse{200, "bad JSON"}); });
  HttpClient client;
  ProviderRegistry registry;
  config.key.clear();
  auto missing = registry.create(config, client);
  requireError("provider_not_configured", [&] { missing->search(query(), token); });
}

void structuredSearchProviders() {
  const auto token = std::make_shared<CancelToken>();
  const std::string key = "example-fake-key";
  const auto checkRequest = [&](const HttpRequest &outgoing, const auto &received, std::string_view url) {
    require(outgoing.url == url && outgoing.method == HttpMethod::Post && outgoing.contentType == "application/json",
            "Structured search must use a JSON POST to its own official endpoint.");
    require(outgoing.headers.at("Authorization") == "Bearer " + key && outgoing.redirectHosts.empty(),
            "Structured search credential must stay on its own endpoint.");
    require(received == token, "Structured search did not propagate its cancellation token.");
    return parseBoundedJson(outgoing.body);
  };

  ProviderConfig perplexity{"perplexity", {}, {}, key, {}};
  const auto perplexityResult = searchPerplexity(query(), perplexity, [&](HttpRequest outgoing, const auto &received) {
    const auto args = checkRequest(outgoing, received, "https://api.perplexity.ai/search");
    require(args == Json{{"query", "C++ & QGIS"}, {"max_results", 4}, {"search_recency_filter", "week"},
                         {"search_domain_filter", Json::array({"example.com"})},
                         {"search_language_filter", Json::array({"zh"})}}, "Incorrect Perplexity query and filters.");
    return jsonResponse({{"results", Json::array({{{"url", "https://example.com/perplexity"}, {"title", "Search"},
                                                       {"snippet", "Perplexity snippet"}, {"date", "2025-01-23"}},
                                                      {{"title", "Missing URL"}}})}});
  }, token);
  require(perplexityResult.source == "perplexity" && perplexityResult.page["results"] == Json::array({
      resultItem("perplexity", "https://example.com/perplexity", "Search", "Perplexity snippet", "2025-01-23")}),
          "Incorrect Perplexity results.");

  ProviderConfig deepseek{"deepseek", {}, {}, key, {}};
  auto deepseekRequest = query();
  deepseekRequest.arguments["domains"].push_back("example.net");
  const auto deepseekResult = searchDeepSeek(deepseekRequest, deepseek, [&](HttpRequest outgoing, const auto &received) {
    const Json args = checkRequest(outgoing, received, "https://api.deepseek.com/v1/tools/search");
    require(args["query"].get<std::string>().starts_with("C++ & QGIS site:example.com OR site:example.net lang:zh after:") &&
                args["count"] == 4 && args["need_content"] == false, "Incorrect DeepSeek query and filters.");
    return jsonResponse({{"results", Json::array({{{"url", "https://example.com/deepseek"}, {"title", "Search"},
                                                       {"snippet", "DeepSeek snippet"}, {"summary", "Longer summary"},
                                                       {"date", "2026-06-01T00:00:00+00:00"}},
                                                      {{"url", "https://example.net/no-snippet"}, {"summary", "Summary"}}})}});
  }, token);
  require(deepseekResult.source == "deepseek" && deepseekResult.page["results"] == Json::array({
      resultItem("deepseek", "https://example.com/deepseek", "Search", "DeepSeek snippet", "2026-06-01T00:00:00+00:00"),
      resultItem("deepseek", "https://example.net/no-snippet", "", "Summary")}), "Incorrect DeepSeek results.");

  const auto globe = QString::fromUcs4(U"\U0001F310");
  deepseekRequest.arguments["query"] = globe.repeated(128).toStdString();
  int sends = 0;
  const auto sendBoundary = [&](HttpRequest outgoing, const auto &) {
    ++sends;
    require(parseBoundedJson(outgoing.body)["query"] == deepseekRequest.arguments["query"],
            "DeepSeek must preserve a query of 128 Unicode code points even if optional hints do not fit.");
    return jsonResponse({{"results", Json::array()}});
  };
  require(searchDeepSeek(deepseekRequest, deepseek, sendBoundary, token).page["results"].empty(),
          "DeepSeek empty results must succeed.");
  deepseekRequest.arguments["query"] = globe.repeated(129).toStdString();
  requireError("invalid_argument", [&] { (void)searchDeepSeek(deepseekRequest, deepseek, sendBoundary, token); });
  require(sends == 1, "Too-long DeepSeek query must be rejected before network traffic.");

  HttpClient client;
  ProviderRegistry registry;
  const auto queryWithDefaults = query();
  for (const auto &[search, config] : {std::pair{searchPerplexity, perplexity}, std::pair{searchDeepSeek, deepseek}}) {
    const auto empty = search(queryWithDefaults, config, [](HttpRequest, const auto &) {
      return jsonResponse({{"results", Json::array()}});
    }, token);
    require(empty.page["results"].empty(), "An empty upstream result must succeed.");
    requireError("rate_limited", [&] {
      (void)search(queryWithDefaults, config, [](HttpRequest, const auto &) { return HttpResponse{429}; }, token);
    });
    requireError("provider_unavailable", [&] {
      (void)search(queryWithDefaults, config, [](HttpRequest, const auto &) { return HttpResponse{200, "{}"}; }, token);
    });
    requireError("timeout", [&] {
      (void)search(queryWithDefaults, config, [](HttpRequest, const auto &) -> HttpResponse {
        throw Error("timeout", "Fixture timeout.", 504, true);
      }, token);
    });
  }
  for (auto config : {perplexity, deepseek}) {
    config.key.clear();
    auto provider = registry.create(config, client);
    requireError("provider_not_configured", [&] { provider->search(query(), token); });
  }
}
} // namespace

int main(int argc, char **argv) {
  QCoreApplication app(argc, argv);
  try {
    require(argc == 2, "Expected a fixture directory.");
    transportValidation();
    serpFixtures(argv[1]);
    autoFallback(argv[1]);
    pacingLimits();
    keyedSearch();
    structuredSearchProviders();
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  return 0;
}
