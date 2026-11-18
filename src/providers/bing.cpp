// Bing fallback follows Strata's MIT-licensed source ordering.
// See packaging/licenses/strata/LICENSE.
#include "providers_internal.hpp"

#include <QUrlQuery>

namespace qws {
HttpRequest bingRequest(const SearchRequest &request) {
  const auto locale = searchLocale(request);
  QUrlQuery query;
  query.addQueryItem("q", queryWithDomains(request));
  if (!locale.language.isEmpty())
    query.addQueryItem("setlang", locale.language);
  if (!locale.region.isEmpty())
    query.addQueryItem("mkt", locale.tag());
  HttpRequest upstream;
  upstream.url = "https://www.bing.com/search?" + formEncode(query);
  upstream.redirectHosts = {"www.bing.com", "cn.bing.com"};
  if (!locale.language.isEmpty())
    upstream.headers["Accept-Language"] = locale.tag().toStdString();
  return upstream;
}
} // namespace qws
