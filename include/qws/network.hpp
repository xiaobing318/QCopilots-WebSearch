#pragma once
#include <qws/common.hpp>
#include <QHostAddress>
#include <map>

namespace qws {
struct HttpResponse {
  int status = 0;
  std::string body, contentType, url;
  std::map<std::string, std::string> headers;
};
class HttpClient {
public:
  explicit HttpClient(std::string backendCaFile = {});
  HttpResponse getPublic(std::string url, std::shared_ptr<CancelToken> cancel) const;
  HttpResponse requestBackend(std::string url, std::string formBody,
      std::string bearerToken, std::shared_ptr<CancelToken> cancel,
      std::string caFile = {}) const;
  static bool isPublicAddress(const QHostAddress &address);
private:
  std::string backendCaFile_;
};
}
