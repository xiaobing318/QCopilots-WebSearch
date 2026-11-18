#include "../../src/providers/serp_parsers.hpp"
#include <QUrl>
#include <array>
#include <cstdint>
#include <cstdlib>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, std::size_t size) {
  if (size < 2 || size > 1024 * 1024 + 1)
    return 0;
  constexpr std::array sources{qws::SerpSource::DuckDuckGoHtml, qws::SerpSource::DuckDuckGoLite,
                               qws::SerpSource::Bing};
  constexpr std::array urls{"https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/",
                            "https://www.bing.com/search"};
  const auto source = data[0] % sources.size();
  const auto result = qws::parseSerp(sources[source],
      {reinterpret_cast<const char *>(data + 1), size - 1}, urls[source]);
  if (!result.results.is_array() || result.results.size() > 100 ||
      (!result.results.empty() && (!result.recognized || result.blocked)))
    std::abort();

  for (const auto &item : result.results) {
    const auto url = item.at("url").get<std::string>();
    const QUrl parsed(QString::fromStdString(url), QUrl::StrictMode);
    const auto title = item.at("title").get<std::string>();
    const auto snippet = item.at("content").get<std::string>();
    if (url.size() > 4096 || !parsed.isValid() || parsed.hasFragment() || parsed.host().isEmpty() ||
        parsed.authority(QUrl::FullyEncoded).contains('@') ||
        (parsed.scheme() != "https" && parsed.scheme() != "http") ||
        title.empty() || title.size() > 512 * 4 || snippet.size() > 2048 * 4 ||
        QString::fromStdString(title).toStdString() != title ||
        QString::fromStdString(snippet).toStdString() != snippet)
      std::abort();
  }
  return 0;
}
