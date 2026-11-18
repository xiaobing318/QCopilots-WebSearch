#pragma once
#include <QHostAddress>
#include <QUrl>
#include <map>
#include <qws/common.hpp>
#include <vector>

namespace qws {
// Syntax normalization only. Does not resolve or authorize a destination.
QUrl normalizeHttpUrl(const std::string &value);
enum class HttpMethod { Get, Post };
struct HttpRequest {
  std::string url;
  HttpMethod method = HttpMethod::Get;
  std::string body, contentType;
  std::map<std::string, std::string> headers;
  // Optional exact, normalized destination hosts for HTTPS GET redirects.
  std::vector<std::string> redirectHosts;
  std::chrono::milliseconds timeout{0};
};
struct HttpResponse {
  int status = 0;
  std::string body, contentType, url;
  std::map<std::string, std::string> headers;
};
int retryAfterSeconds(const HttpResponse &response);
class HttpClient {
public:
  explicit HttpClient(std::string backendCaFile = {});
  HttpResponse send(HttpRequest request, std::shared_ptr<CancelToken> cancel) const;
  HttpResponse getPublic(std::string url, std::shared_ptr<CancelToken> cancel) const;
  HttpResponse requestBackend(std::string url, std::string formBody, std::string bearerToken,
                              std::shared_ptr<CancelToken> cancel, std::string caFile = {}) const;
  static bool isPublicAddress(const QHostAddress &address);

private:
  std::string backendCaFile_;
};
} // namespace qws
