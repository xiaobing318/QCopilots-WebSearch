#include "listener_options.hpp"
#include <QFile>
#include <QFileInfo>
#include <QStringList>
#include <QUrl>
#include <algorithm>
#include <condition_variable>
#include <httplib.h>
#include <mutex>
#include <new>
#include <optional>
#include <qws/admin.hpp>
#include <qws/client_registry.hpp>
#include <qws/server.hpp>
#if defined(_WIN32) && defined(CPPHTTPLIB_MBEDTLS_SUPPORT)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <mbedtls/threading.h>
#include <psa/crypto.h>
#endif
namespace qws {
namespace {
void sendJson(httplib::Response &response, const Json &payload, int status = 200) {
  auto encoded = payload.dump();
  if (encoded.size() > 1024 * 1024) {
    response.status = 500;
    response.set_content(
        errorPayload(Error("internal_error", "Response exceeded its wire budget.", 500)).dump(),
        "application/json");
    return;
  }
  response.status = status;
  response.set_content(encoded, "application/json");
  if (status == 429)
    response.set_header("Retry-After", std::to_string(
        payload.contains("error") ? payload["error"].value("retry_after_seconds", 1) : 1));
  if (status == 401)
    response.set_header("WWW-Authenticate", "Bearer");
}
void sendError(httplib::Response &response, const Error &error) {
  sendJson(response, errorPayload(error), error.status);
}
bool contains(const Json &array, const std::string &value, bool insensitive = false) {
  return std::any_of(array.begin(), array.end(), [&](const Json &v) {
    return insensitive ? QString::fromStdString(v.get<std::string>())
                                 .compare(QString::fromStdString(value), Qt::CaseInsensitive) == 0
                       : v == value;
  });
}
bool validBusinessOrigin(const std::string &origin) {
  // Browser origins are single ASCII HTTP(S) authorities, not general URLs.
  // Check the wire value before QUrl can normalize whitespace or escapes.
  if (origin.empty() || origin.size() > 300 ||
      std::any_of(origin.begin(), origin.end(), [](unsigned char ch) {
        return ch <= 0x20 || ch >= 0x7f || ch == '\\' || ch == ',' || ch == '%' || ch == '@';
      }))
    return false;
  const QUrl url(QString::fromStdString(origin), QUrl::StrictMode);
  if (!url.isValid() || (url.scheme() != "http" && url.scheme() != "https") || url.host().isEmpty() ||
      !url.userInfo().isEmpty() || !url.path().isEmpty() || url.hasQuery() || url.hasFragment())
    return false;
  const auto separator = origin.find("://");
  if (separator == std::string::npos)
    return false;
  const auto colon = origin.rfind(':');
  const auto bracket = origin.rfind(']');
  if (colon > separator && (bracket == std::string::npos || colon > bracket)) {
    if (colon + 1 == origin.size())
      return false;
    unsigned int port = 0;
    for (auto i = colon + 1; i < origin.size(); ++i) {
      const auto ch = origin[i];
      if (ch < '0' || ch > '9')
        return false;
      port = port * 10 + static_cast<unsigned int>(ch - '0');
      if (port > 65535)
        return false;
    }
  }
  return true;
}
bool accepts(const std::string &header, const char *mime) {
  for (const auto &part : QString::fromStdString(header).split(',')) {
    if (part.section(';', 0, 0).trimmed().compare(QString::fromLatin1(mime), Qt::CaseInsensitive) != 0)
      continue;
    bool enabled = true;
    for (const auto &parameter : part.split(';').mid(1))
      if (parameter.section('=', 0, 0).trimmed().compare(QLatin1StringView("q"), Qt::CaseInsensitive) == 0) {
        bool valid = false;
        const auto q = parameter.section('=', 1).trimmed().toDouble(&valid);
        if (!valid || q <= 0)
          enabled = false;
      }
    if (enabled)
      return true;
  }
  return false;
}
bool acceptsResponseType(const httplib::Request &req, const char *mime) {
  const auto [first, last] = req.headers.equal_range("Accept");
  return std::any_of(first, last, [mime](const auto &header) { return accepts(header.second, mime); });
}
bool needsDisconnectMonitor(const Json &message) {
  if (!message.is_object() || !message.contains("id") || !Protocol::validId(message.at("id")) ||
      message.value("method", Json()) != "tools/call")
    return false;
  const auto params = message.find("params");
  if (params == message.end() || !params->is_object())
    return false;
  const auto name = params->find("name");
  return name != params->end() && (*name == "web_search" || *name == "web_open");
}
struct DisconnectMonitor {
  std::mutex mutex;
  std::condition_variable_any wake;
  std::jthread thread;
  DisconnectMonitor(const httplib::Request &request, const std::shared_ptr<CancelToken> &token)
      : thread([this, &request, token](std::stop_token stop) {
          std::unique_lock lock(mutex);
          while (!stop.stop_requested()) {
            if (request.is_connection_closed()) {
              token->cancelled = true;
              return;
            }
            wake.wait_for(lock, stop, std::chrono::milliseconds(25), [] { return false; });
          }
        }) {}
};
QByteArray readTlsPem(const Config &config, const Json &tls, const char *field) {
  constexpr qint64 maxBytes = 1024 * 1024;
  const auto &path = tls.at(field).get_ref<const std::string &>();
  QFile file(QString::fromStdString(config.path(path)));
  if (path.find('\0') != std::string::npos || !QFileInfo(file).isFile() || !file.open(QIODevice::ReadOnly))
    throw Error("invalid_configuration", std::string("Cannot open business.tls.") + field + ".");
  auto contents = file.read(maxBytes + 1);
  if (file.error() != QFileDevice::NoError || contents.isEmpty() || contents.size() > maxBytes ||
      contents.contains('\0'))
    throw Error("invalid_configuration",
                std::string("Invalid or oversized PEM file for business.tls.") + field + ".");
  return contents;
}

#if defined(_WIN32) && defined(CPPHTTPLIB_MBEDTLS_SUPPORT)
// A listener's worker threads share its key, random generator and PSA state.
struct MbedTlsRuntime {
  MbedTlsRuntime() {
    mbedtls_threading_set_alt(
        [](mbedtls_threading_mutex_t *mutex) { InitializeSRWLock(mutex); },
        [](mbedtls_threading_mutex_t *) {},
        [](mbedtls_threading_mutex_t *mutex) {
          AcquireSRWLockExclusive(mutex);
          return 0;
        },
        [](mbedtls_threading_mutex_t *mutex) {
          ReleaseSRWLockExclusive(mutex);
          return 0;
        });
    if (psa_crypto_init() != PSA_SUCCESS) {
      mbedtls_threading_free_alt();
      throw Error("invalid_configuration", "Cannot initialize TLS cryptography.");
    }
  }
  ~MbedTlsRuntime() {
    mbedtls_psa_crypto_free();
    mbedtls_threading_free_alt();
  }
};
#endif

#ifdef CPPHTTPLIB_OPENSSL_SUPPORT
void addIntermediateCertificates(httplib::SSLServer &server, const QByteArray &chain) {
  // cpp-httplib's OpenSSL in-memory loader installs the leaf only. Mbed TLS
  // consumes the entire PEM chain in the SSLServer constructor.
  const auto count = chain.count("-----BEGIN CERTIFICATE-----");
  if (count < 2)
    return;
  std::unique_ptr<BIO, decltype(&BIO_free)> input(BIO_new_mem_buf(chain.constData(), int(chain.size())),
                                                   BIO_free);
  if (!input)
    throw Error("invalid_configuration", "Cannot load the business TLS certificate chain.");
  auto *context = static_cast<SSL_CTX *>(server.tls_context());
  for (qsizetype i = 0; i < count; ++i) {
    std::unique_ptr<X509, decltype(&X509_free)> cert(PEM_read_bio_X509(input.get(), nullptr, nullptr, nullptr),
                                                     X509_free);
    if (!cert || (i > 0 && SSL_CTX_add1_chain_cert(context, cert.get()) != 1))
      throw Error("invalid_configuration", "Cannot load the business TLS certificate chain.");
  }
}
#endif

std::unique_ptr<httplib::Server> businessListener(const Config &config) {
  const auto &business = config.value.at("business");
  if (!business.contains("tls"))
    return std::make_unique<httplib::Server>();
  const auto &tls = business.at("tls");
  const auto chain = readTlsPem(config, tls, "certificate_chain_file");
  const auto key = readTlsPem(config, tls, "private_key_file");
#if defined(_WIN32) && defined(CPPHTTPLIB_MBEDTLS_SUPPORT)
  static const MbedTlsRuntime runtime;
#endif
  // Supplying an empty password prevents an interactive OpenSSL passphrase
  // prompt when an encrypted key is accidentally configured for the service.
  auto server = std::make_unique<httplib::SSLServer>(httplib::SSLServer::PemMemory{
      chain.constData(), size_t(chain.size()), key.constData(), size_t(key.size()), nullptr, 0, ""});
  if (!server->is_valid())
    throw Error("invalid_configuration", "Cannot load the business TLS certificate and matching private key.");
#ifdef CPPHTTPLIB_OPENSSL_SUPPORT
  addIntermediateCertificates(*server, chain);
#endif
  return server;
}
} // namespace
void checkBusinessTls(const Config &config) {
  if (config.value.at("business").contains("tls"))
    (void)businessListener(config);
}
struct HttpServer::Impl {
  Service &service;
  Config config;
  Protocol protocol;
  ClientRegistry clients;
  AdminServer admin;
  std::unique_ptr<httplib::Server> business;
  httplib::Server control;
  std::thread businessThread, controlThread;
  int bp = 0, cp = 0;
  bool started = false;
  enum class ListenerState { Active, Stopping, Failed };
  std::atomic<ListenerState> listenerState{ListenerState::Active};
  Impl(Service &s, Config c)
      : service(s), config(std::move(c)), protocol(s), clients(config), admin(s, config, clients),
        business(businessListener(config)) {
    configureListener(*business, config.value.at("business"), false);
    configureListener(control, config.value.at("control"), true);
    registerMcpRoutes(*business);
    registerControlRoutes(control);
  }
  std::string authorize(const httplib::Request &request) const {
    if (request.get_header_value_count("Authorization") != 1)
      throw Error("unauthorized", "Exactly one bearer credential is required.", 401);
    return clients.authorize(request.get_header_value("Authorization"));
  }
  static const std::string &principal(const httplib::Response &response) {
    const auto *value = response.user_data.get<std::string>("qcws.principal");
    if (!value)
      throw Error("unauthorized", "A bearer credential is required.", 401);
    return *value;
  }
  void configureListener(httplib::Server &server, Json listener, bool controls) {
    server.set_socket_options(listenerSocketOptions);
    const auto threads = controls ? 4 : config.value["limits"]["max_concurrent"].get<int>() + 4;
    server.new_task_queue = [threads] { return new httplib::ThreadPool(threads, threads, 16); };
    server.set_read_timeout(2)
        .set_write_timeout(2)
        .set_keep_alive_max_count(4)
        .set_keep_alive_timeout(1)
        .set_payload_max_length(1024 * 1024)
        .set_compress(false);
    server.set_default_headers({{"Cache-Control", "no-store"}, {"X-Content-Type-Options", "nosniff"}});
    server.set_pre_routing_handler(
        [this, listener, controls](const httplib::Request &req, httplib::Response &res) {
          try {
            const bool hasBody =
                req.has_header("Transfer-Encoding") ||
                (req.has_header("Content-Length") && req.get_header_value("Content-Length") != "0");
            if ((controls || req.method != "POST") && hasBody) {
              res.set_header("Connection", "close");
              throw Error("invalid_argument", "Request body is not permitted on this endpoint.", 400);
            }
            if (req.get_header_value_count("Host") != 1 ||
                !contains(listener["allowed_hosts"], req.get_header_value("Host"), true))
              throw Error("forbidden", "Host is not allowed.", 403);
            if (req.get_header_value_count("Origin") > 1)
              throw Error("forbidden", "Origin is not allowed.", 403);
            if (req.has_header("Origin")) {
              const auto origin = req.get_header_value("Origin");
              if (controls ? !contains(listener["origins"], origin) : !validBusinessOrigin(origin))
                throw Error("forbidden", "Origin is not allowed.", 403);
              if (controls || req.path == "/mcp") {
                res.set_header("Access-Control-Allow-Origin", origin);
                res.set_header("Vary", "Origin");
                res.set_header("Access-Control-Expose-Headers", "Mcp-Session-Id");
              }
            }
            if (req.method == "OPTIONS") {
              if (!controls && req.path != "/mcp")
                throw Error("http_error", "HTTP request could not be handled.", 404);
              if (!req.has_header("Origin"))
                throw Error("forbidden", "Browser preflight requires an allowed Origin.", 403);
              res.status = 204;
              res.set_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
              std::string headers = "Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version";
              if (!controls)
                headers += ", Mcp-Method, Mcp-Name";
              res.set_header("Access-Control-Allow-Headers", headers);
              res.set_header("Access-Control-Max-Age", "600");
              return httplib::Server::HandlerResponse::Handled;
            }
            res.user_data.set("qcws.principal", authorize(req));
            return httplib::Server::HandlerResponse::Unhandled;
          } catch (const Error &e) {
            sendError(res, e);
            return httplib::Server::HandlerResponse::Handled;
          }
        });
    server.set_error_handler([](const httplib::Request &, httplib::Response &res) {
      if (!res.body.empty())
        return;
      if (res.status == 413)
        sendError(res, Error("request_too_large", "Request body exceeds 1 MiB.", 413));
      else
        sendError(res, Error("http_error", "HTTP request could not be handled.", res.status));
    });
    server.set_exception_handler([](const httplib::Request &, httplib::Response &res, std::exception_ptr) {
      sendError(res, Error("internal_error", "Internal server error.", 500));
    });
  }
  void registerControlRoutes(httplib::Server &server) {
    server.Get("/health/live",
               [](const auto &, auto &res) { sendJson(res, {{"contract_version", 1}, {"live", true}}); });
    server.Get("/health/ready", [this](const auto &, auto &res) {
      auto status = service.status();
      if (listenerState.load() == ListenerState::Failed)
        status["service"] = "stopping";
      sendJson(res, status, status["service"] == "ready" ? 200 : 503);
    });
    server.Get("/metrics", [this](const auto &, auto &res) {
      const auto s = service.status();
      res.set_content("# TYPE qcws_active_calls gauge\nqcws_active_calls " + s["active_calls"].dump() +
                          "\n# TYPE qcws_active_upstream gauge\nqcws_active_upstream " +
                          s["active_upstream"].dump() + "\n",
                      "text/plain; version=0.0.4");
    });
  }
  void registerMcpRoutes(httplib::Server &server) {
    server.Get("/mcp", [this](const auto &req, auto &res) {
      if (!protocolHeaders(req, res))
        return;
      if (req.has_header("Mcp-Session-Id")) {
        const int status = protocol.sessionStatus(principal(res), req.get_header_value("Mcp-Session-Id"));
        if (status != 200) {
          res.status = status;
          return;
        }
      }
      res.status = 405;
      res.set_header("Allow", "POST, DELETE");
    });
    server.Delete("/mcp", [this](const auto &req, auto &res) {
      if (!protocolHeaders(req, res))
        return;
      const auto reply = protocol.deleteSession(principal(res), req.get_header_value("Mcp-Session-Id"));
      res.status = reply.httpStatus;
    });
    server.Post("/mcp", [this](const auto &req, auto &res) {
      if (!protocolHeaders(req, res))
        return;
      if (req.get_header_value_count("Content-Type") != 1 ||
          !accepts(req.get_header_value("Content-Type"), "application/json")) {
        sendError(res, Error("invalid_argument", "Content-Type application/json is required.", 415));
        return;
      }
      if (!acceptsResponseType(req, "application/json") || !acceptsResponseType(req, "text/event-stream")) {
        sendError(res, Error("invalid_argument",
                             "Accept must include application/json and text/event-stream.", 406));
        return;
      }
      auto message = parseBoundedJson(req.body);
      if (message.is_discarded()) {
        sendJson(
            res,
            {{"jsonrpc", "2.0"}, {"id", nullptr}, {"error", {{"code", -32700}, {"message", "Parse error"}}}},
            400);
        return;
      }
      auto token = std::make_shared<CancelToken>();
      token->deadline = Clock::now() + std::chrono::seconds(60);
      std::optional<DisconnectMonitor> monitor;
      if (needsDisconnectMonitor(message))
        monitor.emplace(req, token);
      const auto reply =
          protocol.handle(principal(res), req.get_header_value("Mcp-Session-Id"), message, token);
      if (!reply.sessionId.empty())
        res.set_header("Mcp-Session-Id", reply.sessionId);
      if (reply.message)
        sendJson(res, *reply.message, reply.httpStatus);
      else
        res.status = reply.httpStatus;
    });
  }
  static bool protocolHeaders(const httplib::Request &req, httplib::Response &res) {
    // An absent header is accepted. A present one must name a supported revision.
    if (req.get_header_value_count("Mcp-Session-Id") > 1 ||
        req.get_header_value_count("MCP-Protocol-Version") > 1 ||
        (req.has_header("MCP-Protocol-Version") &&
         !Protocol::supportsVersion(req.get_header_value("MCP-Protocol-Version")))) {
      sendError(res, Error("invalid_argument", "Unsupported protocol header."));
      return false;
    }
    return true;
  }
  void listenerExited() {
    auto expected = ListenerState::Active;
    if (listenerState.compare_exchange_strong(expected, ListenerState::Failed))
      service.shutdown();
  }
  void start() {
    auto bind = [](httplib::Server &s, const Json &c) {
      const int p = c["port"];
      const std::string host = c["host"];
      if (p == 0)
        return s.bind_to_any_port(host);
      return s.bind_to_port(host, p) ? p : -1;
    };
    bp = bind(*business, config.value["business"]);
    if (bp < 0)
      throw Error("invalid_configuration", "Cannot bind business listener.");
    try {
      cp = bind(control, config.value["control"]);
      if (cp < 0)
        throw Error("invalid_configuration", "Cannot bind control listener.");
    } catch (...) {
      business->stop();
      control.stop();
      throw;
    }
    started = true;
    try {
      const auto onExit = [this] { listenerExited(); };
      businessThread = std::thread([this, onExit] { runListener(*business, onExit); });
      controlThread = std::thread([this, onExit] { runListener(control, onExit); });
      business->wait_until_ready();
      control.wait_until_ready();
      if (!business->is_running() || !control.is_running())
        throw Error("service_error", "HTTP listener stopped during startup.", 503);
      admin.start(bp, onExit);
      if (listenerState.load() == ListenerState::Failed)
        throw Error("service_error", "HTTP listener stopped during startup.", 503);
    } catch (...) {
      stop();
      throw;
    }
  }
  void stop() {
    if (!started)
      return;
    auto expected = ListenerState::Active;
    listenerState.compare_exchange_strong(expected, ListenerState::Stopping);
    protocol.shutdown();
    service.shutdown();
    admin.stop();
    business->stop();
    control.stop();
    if (businessThread.joinable())
      businessThread.join();
    if (controlThread.joinable())
      controlThread.join();
    started = false;
  }
};
HttpServer::HttpServer(Service &s, Config c) : d_(std::make_unique<Impl>(s, std::move(c))) {}
HttpServer::~HttpServer() {
  stop();
}
void HttpServer::start() {
  d_->start();
}
void HttpServer::stop() {
  d_->stop();
}
bool HttpServer::listenerFailed() const {
  return d_->listenerState.load() == Impl::ListenerState::Failed;
}
#ifdef QCWS_TEST_LISTENER_ACCESS
void failBusinessAcceptForTest(HttpServer &server) {
  class FailingQueue final : public httplib::TaskQueue {
  public:
    bool enqueue(std::function<void()>) override { throw std::bad_alloc(); }
    void shutdown() override {}
  };
  // The first accepted connection fails dispatch. httplib closes the socket
  // and returns false from the actual accept loop with no HttpServer::stop().
  server.d_->business->new_task_queue = [] { return new FailingQueue; };
}
#endif
int HttpServer::businessPort() const {
  return d_->bp;
}
int HttpServer::controlPort() const {
  return d_->cp;
}
} // namespace qws
