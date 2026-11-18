#include "providers_internal.hpp"

#include <QUrlQuery>
#include <algorithm>

namespace qws {
QString SearchLocale::tag() const {
  return region.isEmpty() ? language : language + '-' + region;
}

SearchLocale searchLocale(const SearchRequest &request) {
  const auto tag = QString::fromStdString(request.arguments["language"].get<std::string>());
  if (tag.compare("all", Qt::CaseInsensitive) == 0)
    return {};
  auto language = tag.section('-', 0, 0).toLower();
  const auto suffix = tag.section('-', 1, 1);
  auto region = suffix.size() == 2 ? suffix.toUpper() : QString();
  if (language == "zh" && region.isEmpty())
    region = suffix.compare("Hant", Qt::CaseInsensitive) == 0 ? "TW" : "CN";
  return {std::move(language), std::move(region)};
}

QString queryWithDomains(const SearchRequest &request) {
  auto query = QString::fromStdString(request.arguments["query"].get<std::string>());
  QStringList sites;
  for (const auto &value : request.arguments["domains"])
    sites.push_back("site:" + QString::fromStdString(value.get<std::string>()));
  if (!sites.isEmpty())
    query += " (" + sites.join(" OR ") + ')';
  return query;
}

std::string formEncode(const QUrlQuery &query) {
  QByteArray encoded;
  for (const auto &pair : query.queryItems(QUrl::FullyDecoded)) {
    if (!encoded.isEmpty())
      encoded += '&';
    encoded += QUrl::toPercentEncoding(pair.first);
    encoded += '=';
    encoded += QUrl::toPercentEncoding(pair.second);
  }
  return encoded.toStdString();
}
} // namespace qws
