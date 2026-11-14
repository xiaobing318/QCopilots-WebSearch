#pragma once
#include <QHostAddress>
#include <QUrl>
#include <map>
#include <qws/common.hpp>

namespace qws {
// Syntax normalization only. Does not resolve or authorize a destination.
QUrl normalizeHttpUrl(const std::string &value);
struct HttpResponse {
  int status = 0;
  std::string body, contentType, url;
  std::map<std::string, std::string> headers;
};
class HttpClient {
public:
  explicit HttpClient(std::string backendCaFile = {});
  HttpResponse getPublic(std::string url, std::shared_ptr<CancelToken> cancel) const;
  HttpResponse requestBackend(std::string url, std::string formBody, std::string bearerToken,
                              std::shared_ptr<CancelToken> cancel, std::string caFile = {}) const;
  static bool isPublicAddress(const QHostAddress &address);

private:
  std::string backendCaFile_;
};
} // namespace qws
