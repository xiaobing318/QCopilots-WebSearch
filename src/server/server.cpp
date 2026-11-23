#include "stdio_output.hpp"
#include "listener_options.hpp"
#include <QFile>
#include <QFileInfo>
#include <QStringList>
#include <QUrl>
#include <QUuid>
#include <httplib.h>
#include <iostream>
#include <qws/server.hpp>
#include <qws/admin.hpp>
#include <qws/client_registry.hpp>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#ifdef CPPHTTPLIB_MBEDTLS_SUPPORT
#include <mbedtls/threading.h>
#include <psa/crypto.h>
#endif
#else
#include <cerrno>
#include <poll.h>
#include <unistd.h>
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
      if (parameter.section('=', 0, 0).trimmed() == "q") {
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
struct DisconnectMonitor {
  std::jthread thread;
  DisconnectMonitor(const httplib::Request &request, const std::shared_ptr<CancelToken> &token)
      : thread([&request, token](std::stop_token stop) {
          while (!stop.stop_requested()) {
            if (request.is_connection_closed()) {
              token->cancelled = true;
              return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
          }
        }) {}
};
Json parse(const std::string &body) {
  auto j = parseBoundedJson(body);
  if (j.is_discarded())
    throw Error("invalid_argument", "Request must contain valid UTF-8 JSON.");
  return j;
}

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
  Impl(Service &s, Config c)
      : service(s), config(std::move(c)), protocol(s), clients(config), admin(s, config, clients),
        business(businessListener(config)) {
    setup(*business, config.value["business"], false);
    setup(control, config.value["control"], true);
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
  void setup(httplib::Server &server, Json listener, bool controls) {
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
              res.set_header("Access-Control-Allow-Origin", origin);
              res.set_header("Vary", "Origin");
              res.set_header("Access-Control-Expose-Headers", "Mcp-Session-Id");
            }
            if (req.method == "OPTIONS") {
              if (!req.has_header("Origin"))
                throw Error("forbidden", "Browser preflight requires an allowed Origin.", 403);
              res.status = 204;
              res.set_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
              res.set_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version");
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
    if (controls) {
      server.Get("/health/live",
                 [](const auto &, auto &res) { sendJson(res, {{"contract_version", 1}, {"live", true}}); });
      server.Get("/health/ready", [this](const auto &, auto &res) {
        auto s = service.status();
        sendJson(res, s, s["service"] == "ready" ? 200 : 503);
      });
      server.Get("/metrics", [this](const auto &, auto &res) {
        const auto s = service.status();
        res.set_content("# TYPE qcws_active_calls gauge\nqcws_active_calls " + s["active_calls"].dump() +
                            "\n# TYPE qcws_active_upstream gauge\nqcws_active_upstream " +
                            s["active_upstream"].dump() + "\n",
                        "text/plain; version=0.0.4");
      });
      return;
    }
    server.Get("/v1/status", [this](const auto &, auto &res) { sendJson(res, service.status()); });
    for (const auto &name : {"search", "open", "find"})
      server.Post(std::string("/v1/") + name, [this, name](const auto &req, auto &res) {
        try {
          if (req.get_header_value_count("Content-Type") != 1 ||
              !accepts(req.get_header_value("Content-Type"), "application/json"))
            throw Error("invalid_argument", "Content-Type application/json is required.", 415);
          auto token = std::make_shared<CancelToken>();
          token->deadline = Clock::now() + std::chrono::seconds(60);
          DisconnectMonitor monitor(req, token);
          sendJson(res, service.call(std::string("web_") + name, parse(req.body), principal(res), token));
        } catch (const Error &e) {
          sendError(res, e);
        }
      });
    server.Get("/mcp", [](const auto &, auto &res) {
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
      if (req.get_header_value_count("Content-Type") != 1 ||
          !accepts(req.get_header_value("Content-Type"), "application/json")) {
        sendError(res, Error("invalid_argument", "Content-Type application/json is required.", 415));
        return;
      }
      if (!accepts(req.get_header_value("Accept"), "application/json") ||
          !accepts(req.get_header_value("Accept"), "text/event-stream")) {
        sendError(res, Error("invalid_argument",
                             "Accept must include application/json and text/event-stream.", 406));
        return;
      }
      if (!protocolHeaders(req, res))
        return;
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
      DisconnectMonitor monitor(req, token);
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
    cp = bind(control, config.value["control"]);
    if (cp < 0) {
      business->stop();
      throw Error("invalid_configuration", "Cannot bind control listener.");
    }
    businessThread = std::thread([this] { business->listen_after_bind(); });
    controlThread = std::thread([this] { control.listen_after_bind(); });
    started = true;
    business->wait_until_ready();
    control.wait_until_ready();
    try {
      admin.start(bp);
    } catch (...) {
      stop();
      throw;
    }
  }
  void stop() {
    if (!started)
      return;
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
int HttpServer::businessPort() const {
  return d_->bp;
}
int HttpServer::controlPort() const {
  return d_->cp;
}
int runStdio(Service &service) {
#ifdef _WIN32
  _setmode(_fileno(stdin), _O_BINARY);
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  Protocol protocol(service, QCWS_VERSION, {}, Protocol::TransportPolicy::Stdio);
  std::string session;
  const auto principal = "stdio-" + QUuid::createUuid().toString(QUuid::WithoutBraces).toStdString();
  StdioOutput output;
  auto writeReply = [&](const ProtocolReply &reply, const Json *request = nullptr) {
    auto message = reply.message;
    // HTTP status codes have no channel on stdio. Notifications and cancelled
    // calls still have no response, but rejected requests receive an error.
    if (!message && reply.httpStatus >= 400 && request && request->is_object() &&
        request->contains("method") && (*request)["method"].is_string() && request->contains("id") &&
        Protocol::validId((*request)["id"])) {
      message =
          Json{{"jsonrpc", "2.0"},
               {"id", (*request)["id"]},
               {"error", {{"code", -32002}, {"message", "Session is not initialized or is unavailable."}}}};
    }
    if (message) {
      if (!output.enqueue(message->dump()))
        throw Error("output_unavailable", "Stdio output is unavailable.", 503);
    }
  };
  struct Task {
    std::thread thread;
    std::shared_ptr<std::atomic_bool> done;
  };
  std::vector<Task> tasks;
  std::string line;
  line.reserve(4096);
  std::string pending;
  size_t offset = 0;
  auto reloadAt = Clock::now() + std::chrono::seconds(2);
  const auto reloadSettings = [&] {
    if (Clock::now() >= reloadAt) {
      service.settings().reloadIfChanged();
      reloadAt = Clock::now() + std::chrono::seconds(2);
    }
  };
#ifdef _WIN32
  // A private high-resolution timer avoids Sleep's system-tick latency for
  // sequential MCP calls. Back off while idle without changing timer resolution
  // for other applications. Supported by both Windows deployment targets.
  std::unique_ptr<void, decltype(&CloseHandle)> inputTimer(
      CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                             TIMER_MODIFY_STATE | SYNCHRONIZE),
      &CloseHandle);
  if (!inputTimer)
    throw Error("input_unavailable", "Cannot configure stdio input.", 503);
#endif
  auto readInput = [&]() -> bool {
    char buffer[16384];
#ifdef _WIN32
    LONGLONG delay = 20000; // Relative timer units are 100 ns.
#endif
    for (;;) {
      reloadSettings();
      if (output.failed())
        throw Error("output_unavailable", "Stdio output is unavailable.", 503);
#ifdef _WIN32
      const auto input = GetStdHandle(STD_INPUT_HANDLE);
      DWORD available = sizeof(buffer);
      if (GetFileType(input) == FILE_TYPE_PIPE) {
        if (!PeekNamedPipe(input, nullptr, 0, nullptr, &available, nullptr)) {
          if (GetLastError() == ERROR_BROKEN_PIPE)
            return false;
          throw Error("input_unavailable", "Stdio input is unavailable.", 503);
        }
        if (!available) {
          LARGE_INTEGER due;
          due.QuadPart = -delay;
          if (!SetWaitableTimer(inputTimer.get(), &due, 0, nullptr, nullptr, FALSE) ||
              WaitForSingleObject(inputTimer.get(), 1000) != WAIT_OBJECT_0)
            throw Error("input_unavailable", "Stdio input wait failed.", 503);
          delay = std::min<LONGLONG>(delay * 2, 250000);
          continue;
        }
      }
      DWORD count = 0;
      if (!ReadFile(input, buffer, DWORD(std::min<size_t>(available, sizeof(buffer))), &count, nullptr)) {
        if (GetLastError() == ERROR_BROKEN_PIPE)
          return false;
        throw Error("input_unavailable", "Stdio input is unavailable.", 503);
      }
#else
      pollfd descriptor{STDIN_FILENO, POLLIN, 0};
      const int ready = poll(&descriptor, 1, 25);
      if ((ready < 0 && errno == EINTR) || ready == 0)
        continue;
      if (ready < 0 || (descriptor.revents & (POLLERR | POLLNVAL)))
        throw Error("input_unavailable", "Stdio input is unavailable.", 503);
      const auto count = ::read(STDIN_FILENO, buffer, sizeof(buffer));
      if (count < 0 && errno == EINTR)
        continue;
      if (count < 0)
        throw Error("input_unavailable", "Stdio input is unavailable.", 503);
#endif
      if (!count)
        return false;
      pending.assign(buffer, size_t(count));
      offset = 0;
      return true;
    }
  };
  // A line beyond the 1 MiB input budget is discarded through its newline
  // without buffering, so the process keeps serving later messages.
  enum class Input { Line, Overlong, End };
  auto readLine = [&]() -> Input {
    line.clear();
    bool overlong = false;
    for (;;) {
      if (offset == pending.size() && !readInput())
        return overlong ? Input::Overlong : line.empty() ? Input::End : Input::Line;
      const char ch = pending[offset++];
      if (ch == '\n')
        return overlong ? Input::Overlong : Input::Line;
      if (overlong)
        continue;
      if (line.size() >= 1024 * 1024) {
        overlong = true;
        line.clear();
        continue;
      }
      line += ch;
    }
  };
  int result = 0;
  try {
    while (!output.failed()) {
      reloadSettings();
      const auto input = readLine();
      if (input == Input::End)
        break;
      std::erase_if(tasks, [](Task &t) {
        if (t.done->load()) {
          t.thread.join();
          return true;
        }
        return false;
      });
      if (input == Input::Overlong) {
        writeReply({200,
                    Json{{"jsonrpc", "2.0"},
                         {"id", nullptr},
                         {"error", {{"code", -32600}, {"message", "Message exceeds 1 MiB"}}}},
                    session});
        continue;
      }
      // Blank or CR-only lines are framing noise, not messages.
      if (line.find_first_not_of(" \t\r") == std::string::npos)
        continue;
      auto message = parseBoundedJson(line);
      if (message.is_discarded()) {
        writeReply({200,
                    Json{{"jsonrpc", "2.0"},
                         {"id", nullptr},
                         {"error", {{"code", -32700}, {"message", "Parse error"}}}},
                    session});
        continue;
      }
      if (message.is_object() && message.value("method", Json()) == "tools/call" && tasks.size() < 64) {
        auto done = std::make_shared<std::atomic_bool>(false);
        auto sid = session;
        tasks.push_back({std::thread([&, done, sid, message = std::move(message)] {
                           try {
                             writeReply(protocol.handle(principal, sid, message), &message);
                           } catch (...) {
                             output.fail();
                             protocol.shutdown();
                             service.shutdown();
                           }
                           *done = true;
                         }),
                         done});
      } else {
        auto reply = protocol.handle(principal, session, message);
        if (!reply.sessionId.empty())
          session = reply.sessionId;
        writeReply(reply, &message);
      }
    }
  } catch (const std::exception &) {
    std::cerr << "Stdio input failed.\n";
    result = 1;
  }
  protocol.shutdown();
  service.shutdown();
  for (auto &t : tasks)
    if (t.thread.joinable())
      t.thread.join();
  output.finish();
  return result || output.failed() ? 1 : 0;
}
} // namespace qws
