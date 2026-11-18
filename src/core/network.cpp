#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <netinet/in.h>
#include <sys/socket.h>
#endif
#include <QUrl>
#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <curl/curl.h>
#include <limits>
#include <qws/network.hpp>
#include <string_view>

#ifndef QCWS_VERSION
#define QCWS_VERSION "0.0.0"
#endif

namespace qws {
namespace {
constexpr std::size_t BodyLimit = 5 * 1024 * 1024;
constexpr unsigned RedirectLimit = 5;
constexpr unsigned ProviderRedirectLimit = 3;
constexpr int MaxRetryAfterSeconds = 600;
// Public page failures are attributed to the remote site. Only transient
// conditions are retryable.
[[noreturn]] void remoteFailure(const std::string &message, bool retryable) {
  throw Error("upstream_http_error", message, 502, retryable);
}
constexpr std::size_t HeaderLimit = 64 * 1024;
struct Runtime {
  Runtime() {
    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK)
      throw Error("internal_error", "HTTP runtime initialization failed.", 500);
    const auto *v = curl_version_info(CURLVERSION_NOW);
#ifdef _WIN32
    constexpr std::string_view tlsBackend = "Schannel";
#else
    constexpr std::string_view tlsBackend = "OpenSSL/";
#endif
    if (!v || !v->ares || !(v->features & CURL_VERSION_ASYNCHDNS) || !(v->features & CURL_VERSION_SSL) ||
        !(v->features & CURL_VERSION_LIBZ) || !v->ssl_version ||
        !std::string_view(v->ssl_version).starts_with(tlsBackend)) {
      curl_global_cleanup();
      throw Error("internal_error", "HTTP requires c-ares, the configured TLS backend and gzip.", 500);
    }
  }
  ~Runtime() {
    curl_global_cleanup();
  }
};
void check(CURLcode result) {
  if (result != CURLE_OK)
    throw Error("internal_error", "HTTP setup failed.", 500);
}
template <class T> void option(CURL *h, CURLoption name, T value) {
  const auto result = curl_easy_setopt(h, name, value);
  if (result != CURLE_OK)
    throw Error("internal_error", "HTTP option " + std::to_string(name) + ": " + curl_easy_strerror(result), 500);
}
void checkMulti(CURLMcode result) {
  if (result != CURLM_OK)
    throw Error("provider_unavailable", "HTTP transport failed.", 502, true);
}
void checkCancellation(const std::shared_ptr<CancelToken> &c,
                       Clock::time_point deadline = Clock::time_point::max()) {
  if (!c)
    throw Error("invalid_argument", "A deadline is required.");
  if (c->cancelled.load())
    throw Error("cancelled", "Request cancelled.", 499);
  if (Clock::now() >= std::min(c->deadline, deadline))
    throw Error("timeout", "Request timed out.", 504, true);
}
QUrl normalizedUrl(const std::string &value) {
  if (value.empty() || value.size() > 8192)
    throw Error("invalid_argument", "Invalid HTTP URL.");
  for (unsigned char c : value)
    if (c <= 0x20 || c == 0x7f || c == '\\')
      throw Error("invalid_argument", "Invalid HTTP URL.");
  QUrl url(QString::fromUtf8(value.data(), qsizetype(value.size())), QUrl::StrictMode);
  QString host = url.host(QUrl::FullyDecoded);
  const auto scheme = url.scheme().toLower();
  if (!url.isValid() || url.isRelative() || (scheme != "http" && scheme != "https") || host.isEmpty() ||
      url.authority(QUrl::FullyEncoded).contains('@') || host.contains('%') || url.port(-1) == 0)
    throw Error("invalid_argument", "Invalid HTTP URL.");
  QHostAddress literal;
  if (literal.setAddress(host))
    host = literal.toString();
  else {
    while (host.endsWith('.'))
      host.chop(1);
    const auto ace = QUrl::toAce(host);
    if (ace.isEmpty() || ace.size() > 253)
      throw Error("invalid_argument", "Invalid HTTP host.");
    for (const auto &label : ace.split('.')) {
      if (label.isEmpty() || label.size() > 63 || label.startsWith('-') || label.endsWith('-'))
        throw Error("invalid_argument", "Invalid HTTP host.");
      for (unsigned char c : label)
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-'))
          throw Error("invalid_argument", "Invalid HTTP host.");
    }
    host = QString::fromLatin1(ace).toLower();
  }
  url.setScheme(scheme);
  url.setHost(host);
  url.setFragment(QString());
  if (url.path().isEmpty())
    url.setPath("/");
  return url;
}
std::string trim(std::string_view s) {
  const auto first = s.find_first_not_of(" \t\r\n");
  if (first == std::string_view::npos)
    return {};
  return std::string(s.substr(first, s.find_last_not_of(" \t\r\n") - first + 1));
}
struct Budget {
  std::size_t decoded = 0, headers = 0;
  curl_off_t wire = 0;
};
struct Transfer {
  HttpResponse response;
  std::shared_ptr<CancelToken> cancel;
  Budget &budget;
  bool publicOnly, tooLarge = false, badHeader = false, callbackFailed = false, blocked = false;
  unsigned accepted = 0;
  bool retryAfterConflict = false;
};
bool byteCount(std::size_t s, std::size_t n, std::size_t &bytes) noexcept {
  if (n && s > std::numeric_limits<std::size_t>::max() / n)
    return false;
  bytes = s * n;
  return true;
}
std::size_t receiveBody(char *data, std::size_t s, std::size_t n, void *p) noexcept {
  auto &t = *static_cast<Transfer *>(p);
  try {
    std::size_t bytes = 0;
    if (!byteCount(s, n, bytes) || bytes > BodyLimit - t.budget.decoded) {
      t.tooLarge = true;
      return 0;
    }
    if (t.cancel->stopped())
      return 0;
    t.response.body.append(data, bytes);
    t.budget.decoded += bytes;
    return bytes;
  } catch (...) {
    t.callbackFailed = true;
    return 0;
  }
}
std::size_t receiveHeader(char *data, std::size_t s, std::size_t n, void *p) noexcept {
  auto &t = *static_cast<Transfer *>(p);
  try {
    std::size_t bytes = 0;
    if (!byteCount(s, n, bytes) || bytes > HeaderLimit - t.budget.headers) {
      t.tooLarge = true;
      return 0;
    }
    t.budget.headers += bytes;
    std::string_view line(data, bytes);
    if (line.starts_with("HTTP/")) {
      t.response.headers.clear();
      t.retryAfterConflict = false;
      return bytes;
    }
    const auto colon = line.find(':');
    if (colon == std::string_view::npos)
      return bytes;
    auto name = trim(line.substr(0, colon));
    std::transform(name.begin(), name.end(), name.begin(),
                   [](unsigned char c) { return char(std::tolower(c)); });
    if (name != "location" && name != "content-type" && name != "retry-after")
      return bytes;
    auto value = trim(line.substr(colon + 1));
    // Like browsers, the last Content-Type wins. Conflicting redirect targets
    // remain ambiguous and are rejected.
    const auto old = t.response.headers.find(name);
    if (name == "location" && old != t.response.headers.end() && old->second != value) {
      t.badHeader = true;
      return 0;
    }
    if (name == "retry-after") {
      if (old != t.response.headers.end() && old->second != value) {
        t.retryAfterConflict = true;
        t.response.headers.erase(old);
      }
      if (t.retryAfterConflict)
        return bytes;
    }
    t.response.headers[name] = std::move(value);
    return bytes;
  } catch (...) {
    t.callbackFailed = true;
    return 0;
  }
}
int progress(void *p, curl_off_t, curl_off_t downloaded, curl_off_t, curl_off_t) noexcept {
  auto &t = *static_cast<Transfer *>(p);
  if (downloaded < 0 || downloaded > curl_off_t(BodyLimit) - t.budget.wire) {
    t.tooLarge = true;
    return 1;
  }
  return t.cancel->stopped() ? 1 : 0;
}
curl_socket_t openSocket(void *p, curlsocktype purpose, curl_sockaddr *address) noexcept {
  auto &t = *static_cast<Transfer *>(p);
  try {
    if (purpose != CURLSOCKTYPE_IPCXN || !address)
      return CURL_SOCKET_BAD;
    int socketType = address->socktype;
#ifdef SOCK_CLOEXEC
    socketType &= ~SOCK_CLOEXEC;
#endif
#ifdef SOCK_NONBLOCK
    socketType &= ~SOCK_NONBLOCK;
#endif
    if (socketType != SOCK_STREAM)
      return CURL_SOCKET_BAD;
    QHostAddress ip;
    bool scoped = false;
    if (address->family == AF_INET && address->addrlen >= sizeof(sockaddr_in)) {
      auto *a = reinterpret_cast<const sockaddr_in *>(&address->addr);
      ip = QHostAddress(ntohl(a->sin_addr.s_addr));
    } else if (address->family == AF_INET6 && address->addrlen >= sizeof(sockaddr_in6)) {
      auto *a = reinterpret_cast<const sockaddr_in6 *>(&address->addr);
      Q_IPV6ADDR bytes;
      std::memcpy(bytes.c, &a->sin6_addr, sizeof(bytes.c));
      ip = QHostAddress(bytes);
      scoped = a->sin6_scope_id != 0;
    } else {
      t.blocked = true;
      return CURL_SOCKET_BAD;
    }
    if (t.publicOnly && (scoped || !HttpClient::isPublicAddress(ip))) {
      t.blocked = true;
      return CURL_SOCKET_BAD;
    }
    if (t.cancel->stopped())
      return CURL_SOCKET_BAD;
    ++t.accepted;
    return ::socket(address->family, address->socktype, address->protocol);
  } catch (...) {
    t.callbackFailed = true;
    return CURL_SOCKET_BAD;
  }
}
struct Headers {
  curl_slist *value = nullptr;
  ~Headers() {
    curl_slist_free_all(value);
  }
  void add(const std::string &s) {
    auto *next = curl_slist_append(value, s.c_str());
    if (!next)
      throw Error("internal_error", "HTTP allocation failed.", 500);
    value = next;
  }
};
struct Handles {
  CURL *easy = curl_easy_init();
  CURLM *multi = curl_multi_init();
  bool attached = false;
  ~Handles() {
    if (attached)
      curl_multi_remove_handle(multi, easy);
    if (easy)
      curl_easy_cleanup(easy);
    if (multi)
      curl_multi_cleanup(multi);
  }
};
HttpResponse perform(const QUrl &url, const HttpRequest &request, const std::string &ca,
                     std::shared_ptr<CancelToken> cancel, bool publicOnly, Budget &budget,
                     Clock::time_point deadline = Clock::time_point::max()) {
  checkCancellation(cancel, deadline);
  Transfer t{{}, std::move(cancel), budget, publicOnly};
  t.response.url = url.toEncoded(QUrl::FullyEncoded).toStdString();
  Headers headers;
  Handles h;
  if (!h.easy || !h.multi)
    throw Error("internal_error", "HTTP allocation failed.", 500);
  if (!request.headers.contains("accept"))
    headers.add(publicOnly ? "Accept: text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"
                           : "Accept: application/json");
  headers.add("Expect:");
  for (const auto &[name, value] : request.headers)
    headers.add(name + ": " + value);
  if (!request.contentType.empty())
    headers.add("Content-Type: " + request.contentType);
  option(h.easy, CURLOPT_URL, t.response.url.c_str());
  option(h.easy, CURLOPT_HTTPHEADER, headers.value);
  option(h.easy, CURLOPT_PROTOCOLS_STR, "http,https");
  option(h.easy, CURLOPT_REDIR_PROTOCOLS_STR, "http,https");
  option(h.easy, CURLOPT_FOLLOWLOCATION, 0L);
  option(h.easy, CURLOPT_PROXY, "");
  option(h.easy, CURLOPT_PRE_PROXY, "");
  option(h.easy, CURLOPT_NOPROXY, "*");
  option(h.easy, CURLOPT_SSL_VERIFYPEER, 1L);
  option(h.easy, CURLOPT_SSL_VERIFYHOST, 2L);
  long sslOptions = CURLSSLOPT_NO_PARTIALCHAIN;
#ifdef _WIN32
  // Schannel uses Windows roots by default. Disable online revocation checks.
  sslOptions |= CURLSSLOPT_NO_REVOKE;
#endif
  option(h.easy, CURLOPT_SSL_OPTIONS, sslOptions);
  option(h.easy, CURLOPT_FRESH_CONNECT, 1L);
  option(h.easy, CURLOPT_FORBID_REUSE, 1L);
  // netrc, Alt-Svc and HSTS are disabled at compile time in the pinned curl.
  option(h.easy, CURLOPT_DNS_CACHE_TIMEOUT, 0L);
  option(h.easy, CURLOPT_HTTP09_ALLOWED, 0L);
  option(h.easy, CURLOPT_HTTP_VERSION, long(CURL_HTTP_VERSION_1_1));
  option(h.easy, CURLOPT_NOSIGNAL, 1L);
  option(h.easy, CURLOPT_USERAGENT, "QCopilots-WebSearch/" QCWS_VERSION);
  option(h.easy, CURLOPT_ACCEPT_ENCODING, "gzip");
  option(h.easy, CURLOPT_MAXFILESIZE_LARGE, curl_off_t(BodyLimit));
  option(h.easy, CURLOPT_WRITEFUNCTION, receiveBody);
  option(h.easy, CURLOPT_WRITEDATA, &t);
  option(h.easy, CURLOPT_HEADERFUNCTION, receiveHeader);
  option(h.easy, CURLOPT_HEADERDATA, &t);
  option(h.easy, CURLOPT_NOPROGRESS, 0L);
  option(h.easy, CURLOPT_XFERINFOFUNCTION, progress);
  option(h.easy, CURLOPT_XFERINFODATA, &t);
  option(h.easy, CURLOPT_OPENSOCKETFUNCTION, openSocket);
  option(h.easy, CURLOPT_OPENSOCKETDATA, &t);
  if (!ca.empty()) {
#ifndef _WIN32
    option(h.easy, CURLOPT_CAPATH, static_cast<const char *>(nullptr));
#endif
    option(h.easy, CURLOPT_CAINFO, ca.c_str());
  }
  if (request.method == HttpMethod::Post) {
    option(h.easy, CURLOPT_POSTFIELDSIZE_LARGE, curl_off_t(request.body.size()));
    option(h.easy, CURLOPT_POSTFIELDS, request.body.data());
  }
  const auto remaining =
      std::chrono::duration_cast<std::chrono::milliseconds>(std::min(t.cancel->deadline, deadline) - Clock::now())
          .count();
  if (remaining <= 0)
    throw Error("timeout", "Request timed out.", 504, true);
  const long timeout = long(std::min<std::int64_t>(remaining, std::numeric_limits<long>::max()));
  option(h.easy, CURLOPT_TIMEOUT_MS, timeout);
  option(h.easy, CURLOPT_CONNECTTIMEOUT_MS, std::min(timeout, 10000L));
  checkMulti(curl_multi_add_handle(h.multi, h.easy));
  h.attached = true;
  int running = 0;
  do {
    checkCancellation(t.cancel, deadline);
    checkMulti(curl_multi_perform(h.multi, &running));
    if (running)
      checkMulti(curl_multi_poll(h.multi, nullptr, 0, 50, nullptr));
  } while (running);
  checkCancellation(t.cancel, deadline);
  CURLcode result = CURLE_FAILED_INIT;
  int messages = 0;
  while (auto *m = curl_multi_info_read(h.multi, &messages))
    if (m->msg == CURLMSG_DONE && m->easy_handle == h.easy)
      result = m->data.result;
  curl_off_t wire = 0;
  check(curl_easy_getinfo(h.easy, CURLINFO_SIZE_DOWNLOAD_T, &wire));
  if (wire < 0 || wire > curl_off_t(BodyLimit) - budget.wire)
    t.tooLarge = true;
  else
    budget.wire += wire;
  if (t.tooLarge || result == CURLE_FILESIZE_EXCEEDED)
    throw Error("response_too_large", "Response exceeds its size limit.", 502);
  if (t.callbackFailed)
    throw Error("internal_error", "HTTP callback failed.", 500);
  if (t.badHeader) {
    if (publicOnly)
      remoteFailure("Remote resource returned conflicting headers.", false);
    throw Error("provider_unavailable", "Invalid upstream headers.", 502);
  }
  if (result == CURLE_OPERATION_TIMEDOUT)
    throw Error("timeout", "Request timed out.", 504, true);
  if (result != CURLE_OK) {
    if (t.blocked && t.accepted == 0)
      throw Error("destination_blocked", "URL host resolves only to non-public addresses.", 403);
    if (result == CURLE_BAD_CONTENT_ENCODING)
      throw Error("unsupported_content", "Unsupported content encoding.", 415);
    if (!publicOnly)
      throw Error("provider_unavailable", "Upstream request failed.", 502, true);
    switch (result) {
    case CURLE_COULDNT_RESOLVE_HOST:
      remoteFailure("Remote host name could not be resolved.", false);
    case CURLE_COULDNT_CONNECT:
    case CURLE_SEND_ERROR:
    case CURLE_RECV_ERROR:
    case CURLE_GOT_NOTHING:
    case CURLE_PARTIAL_FILE:
      remoteFailure("Remote connection failed.", true);
    case CURLE_PEER_FAILED_VERIFICATION:
      remoteFailure("TLS verification failed.", false);
    case CURLE_SSL_CONNECT_ERROR:
      remoteFailure("TLS handshake failed.", false);
    case CURLE_WEIRD_SERVER_REPLY:
    case CURLE_UNSUPPORTED_PROTOCOL:
      remoteFailure("Remote server returned an invalid HTTP response.", false);
    default:
      remoteFailure("Remote request failed.", false);
    }
  }
  long status = 0;
  check(curl_easy_getinfo(h.easy, CURLINFO_RESPONSE_CODE, &status));
  t.response.status = int(status);
  if (auto it = t.response.headers.find("content-type"); it != t.response.headers.end())
    t.response.contentType = it->second;
  return std::move(t.response);
}

bool isRedirect(int status) {
  return status == 301 || status == 302 || status == 303 || status == 307 || status == 308;
}

QUrl redirectTarget(const QUrl &url, const HttpResponse &response) {
  const auto location = response.headers.find("location");
  if (location == response.headers.end())
    remoteFailure("Remote resource returned an invalid redirect.", false);
  // Browsers accept unencoded spaces and similar characters in Location.
  const QUrl relative(QString::fromUtf8(location->second.data(), qsizetype(location->second.size())).trimmed(),
                      QUrl::TolerantMode);
  if (!relative.isValid())
    remoteFailure("Remote resource returned an invalid redirect.", false);
  try {
    return normalizedUrl(url.resolved(relative).toEncoded(QUrl::FullyEncoded).toStdString());
  } catch (const Error &) {
    remoteFailure("Remote resource returned an invalid redirect.", false);
  }
}

bool isHttpsOrigin(const QUrl &url) {
  return url.scheme() == "https" && url.port(443) == 443;
}

void validateProviderRequest(HttpRequest &request) {
  if (request.timeout.count() < 0 || request.body.size() > 1024 * 1024 ||
      (request.method == HttpMethod::Get && (!request.body.empty() || !request.contentType.empty())) ||
      (request.method == HttpMethod::Post && request.contentType != "application/json" &&
       request.contentType != "application/x-www-form-urlencoded"))
    throw Error("invalid_argument", "Invalid provider request.");

  std::map<std::string, std::string> normalizedHeaders;
  bool authenticated = false;
  for (const auto &[header, value] : request.headers) {
    auto name = header;
    std::transform(name.begin(), name.end(), name.begin(),
                   [](unsigned char c) { return char(std::tolower(c)); });
    const bool credential = name == "authorization" || name == "x-subscription-token" || name == "x-api-key";
    if ((!credential && name != "accept" && name != "accept-language") || value.empty() ||
        value.size() > 8200 || std::any_of(value.begin(), value.end(),
                                          [](unsigned char c) { return c < 0x20 || c >= 0x7f; }) ||
        !normalizedHeaders.emplace(std::move(name), value).second)
      throw Error("invalid_argument", "Invalid provider request header.");
    authenticated = authenticated || credential;
  }
  if (!request.redirectHosts.empty() && (authenticated || request.method != HttpMethod::Get))
    throw Error("invalid_argument", "Only unauthenticated GET requests can follow provider redirects.");
  request.headers = std::move(normalizedHeaders);
}
} // namespace
QUrl normalizeHttpUrl(const std::string &value) {
  return normalizedUrl(value);
}
int retryAfterSeconds(const HttpResponse &response) {
  const auto header = response.headers.find("retry-after");
  if (header == response.headers.end())
    return 0;
  const auto value = trim(header->second);
  if (value.empty() || value.find('\0') != std::string::npos)
    return 0;
  if (std::all_of(value.begin(), value.end(), [](char c) { return c >= '0' && c <= '9'; })) {
    int seconds = 0;
    for (const char digit : value)
      seconds = std::min(MaxRetryAfterSeconds, seconds * 10 + (digit - '0'));
    return std::max(1, seconds);
  }
  const auto now = std::time(nullptr);
  const auto date = curl_getdate(value.c_str(), nullptr);
  if (now == std::time_t(-1) || date == std::time_t(-1))
    return 0;
  return static_cast<int>(std::clamp(std::difftime(date, now), 1.0, double(MaxRetryAfterSeconds)));
}
HttpClient::HttpClient(std::string ca) : backendCaFile_(std::move(ca)) {
  static Runtime runtime;
  (void)runtime;
}
HttpResponse HttpClient::send(HttpRequest request, std::shared_ptr<CancelToken> cancel) const {
  validateProviderRequest(request);
  auto url = normalizedUrl(request.url);
  if (!isHttpsOrigin(url))
    throw Error("invalid_argument", "Provider requests require HTTPS on port 443.");
  checkCancellation(cancel);
  auto deadline = cancel->deadline;
  const auto now = Clock::now();
  const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
  if (request.timeout.count() > 0 && request.timeout < remaining)
    deadline = now + request.timeout;

  Budget budget;
  for (unsigned redirects = 0;; ++redirects) {
    auto response = perform(url, request, {}, cancel, true, budget, deadline);
    if (!isRedirect(response.status) || request.redirectHosts.empty())
      return response;
    if (redirects >= ProviderRedirectLimit)
      remoteFailure("Provider redirected too many times.", false);
    auto next = redirectTarget(url, response);
    const auto host = next.host().toStdString();
    if (!isHttpsOrigin(next) ||
        std::find(request.redirectHosts.begin(), request.redirectHosts.end(), host) == request.redirectHosts.end())
      remoteFailure("Provider redirected outside its allowed origins.", false);
    url = std::move(next);
  }
}
bool HttpClient::isPublicAddress(const QHostAddress &address) {
  if (address.protocol() == QAbstractSocket::IPv6Protocol) {
    const auto b = address.toIPv6Address();
    bool mapped = true;
    for (int i = 0; i < 10; ++i)
      mapped = mapped && b[i] == 0;
    if (mapped && b[10] == 255 && b[11] == 255) {
      quint32 v = (quint32(b[12]) << 24) | (quint32(b[13]) << 16) | (quint32(b[14]) << 8) | b[15];
      return isPublicAddress(QHostAddress(v));
    }
  }
  if (address.protocol() == QAbstractSocket::IPv4Protocol) {
    static const char *blocked[] = {
        "0.0.0.0/8",       "10.0.0.0/8",     "100.64.0.0/10",  "127.0.0.0/8",     "168.63.129.16/32",
        "169.254.0.0/16",  "172.16.0.0/12",  "192.0.0.0/24",   "192.0.2.0/24",    "192.31.196.0/24",
        "192.52.193.0/24", "192.88.99.0/24", "192.168.0.0/16", "192.175.48.0/24", "198.18.0.0/15",
        "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4",    "240.0.0.0/4"};
    for (auto cidr : blocked)
      if (address.isInSubnet(QHostAddress::parseSubnet(QString::fromLatin1(cidr))))
        return false;
    return true;
  }
  if (address.protocol() != QAbstractSocket::IPv6Protocol || !address.scopeId().isEmpty() ||
      !address.isInSubnet(QHostAddress("2000::"), 3))
    return false;
  for (auto cidr : {"2001::/23", "2001:db8::/32", "2002::/16", "2620:4f:8000::/48", "3fff::/20"})
    if (address.isInSubnet(QHostAddress::parseSubnet(QString::fromLatin1(cidr))))
      return false;
  return true;
}
HttpResponse HttpClient::getPublic(std::string value, std::shared_ptr<CancelToken> cancel) const {
  Budget budget;
  QUrl url = normalizedUrl(value);
  for (unsigned redirects = 0;; ++redirects) {
    auto r = perform(url, {}, {}, cancel, true, budget);
    if (!isRedirect(r.status)) {
      if (r.status < 200 || r.status >= 300)
        remoteFailure("Remote resource returned HTTP " + std::to_string(r.status) + ".",
                      r.status == 429 || r.status >= 500);
      return r;
    }
    if (redirects >= RedirectLimit)
      remoteFailure("Remote resource redirected too many times.", false);
    auto next = redirectTarget(url, r);
    if (url.scheme() == "https" && next.scheme() != "https")
      throw Error("destination_blocked", "HTTPS to HTTP redirects are not permitted.", 403);
    url = next;
  }
}
HttpResponse HttpClient::requestBackend(std::string url, std::string body, std::string token,
                                        std::shared_ptr<CancelToken> cancel, std::string ca) const {
  if (body.size() > 1024 * 1024 || token.size() > 8192)
    throw Error("invalid_argument", "Invalid backend request.");
  for (unsigned char c : token)
    if (c <= 0x20 || c >= 0x7f)
      throw Error("invalid_argument", "Invalid backend credential.");
  Budget budget;
  HttpRequest request;
  if (!body.empty()) {
    request.method = HttpMethod::Post;
    request.contentType = "application/x-www-form-urlencoded";
    request.body = std::move(body);
  }
  if (!token.empty())
    request.headers.emplace("authorization", "Bearer " + token);
  auto r = perform(normalizedUrl(url), request, ca.empty() ? backendCaFile_ : ca, std::move(cancel),
                   false, budget);
  if (r.status >= 300 && r.status < 400)
    throw Error("provider_unavailable", "Backend redirects not permitted.", 502);
  return r;
}
} // namespace qws
