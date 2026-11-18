// DuckDuckGo HTML and Lite source order follows Strata's MIT-licensed
// integration. See packaging/licenses/strata/LICENSE.
#include "providers_internal.hpp"

#include <QUrlQuery>
#include <array>

namespace qws {
namespace {
QString duckDuckGoRegion(const SearchLocale &locale) {
  const auto region = locale.region.toLower();
  auto language = locale.language;
  if (language == "zh" && (region == "tw" || region == "hk"))
    language = "tzh";
  if (language == "ja")
    language = "jp";
  if (language == "ko")
    language = "kr";
  if (language == "nb")
    language = "no";
  const auto code = (region == "gb" ? QString("uk") : region) + '-' + language;
  static constexpr std::array<const char *, 48> regions{{
      "ar-es", "at-de", "au-en", "be-fr", "be-nl", "bg-bg", "br-pt", "ca-en",
      "ca-fr", "ch-de", "ch-fr", "ch-it", "cl-es", "cn-zh", "co-es", "cz-cs",
      "de-de", "dk-da", "ee-et", "es-es", "fi-fi", "fr-fr", "gr-el", "hk-tzh",
      "hr-hr", "hu-hu", "id-en", "id-id", "ie-en", "il-he", "in-en", "it-it",
      "jp-jp", "kr-kr", "mx-es", "nl-nl", "no-no", "nz-en", "pl-pl", "pt-pt",
      "ro-ro", "ru-ru", "se-sv", "sg-en", "tr-tr", "tw-tzh", "uk-en", "us-en",
  }};
  for (const auto *candidate : regions)
    if (code == QLatin1StringView(candidate))
      return code;
  return "wt-wt";
}
} // namespace

HttpRequest duckDuckGoRequest(const SearchRequest &request, SerpSource mode) {
  const auto locale = searchLocale(request);
  QUrlQuery form;
  form.addQueryItem("q", queryWithDomains(request));
  form.addQueryItem("kl", duckDuckGoRegion(locale));
  const auto range = request.arguments["time_range"].get<std::string>();
  if (range != "any")
    form.addQueryItem("df", QString(QChar::fromLatin1(range.front())));
  HttpRequest upstream;
  upstream.url = mode == SerpSource::DuckDuckGoHtml ? "https://html.duckduckgo.com/html/"
                                                   : "https://lite.duckduckgo.com/lite/";
  upstream.method = HttpMethod::Post;
  upstream.body = formEncode(form);
  upstream.contentType = "application/x-www-form-urlencoded";
  if (!locale.language.isEmpty())
    upstream.headers["Accept-Language"] = locale.tag().toStdString();
  return upstream;
}
} // namespace qws
