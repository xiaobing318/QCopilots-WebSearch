#include "admin_sessions.hpp"
#include "static_assets.hpp"
#include "listener_options.hpp"
#include <QString>
#include <qws/admin.hpp>
#include <qws/client_registry.hpp>
#include <thread>

namespace qws {
namespace {
constexpr auto Api = "/admin/api/v1";
constexpr auto Cookie = "qcws_admin";
using Request = httplib::Request;
using Response = httplib::Response;

void sendJson(Response &res, const Json &body, int status = 200) {
  res.status = status;
  res.set_content(body.dump(), "application/json");
}

bool allowed(const Json &list, const std::string &value, bool fold = false) {
  return std::any_of(list.begin(), list.end(), [&](const Json &item) {
    const auto &name = item.get_ref<const std::string &>();
    return fold ? QString::fromStdString(name).compare(QString::fromStdString(value), Qt::CaseInsensitive) == 0
                : name == value;
  });
}

std::string requestCookie(const Request &req) {
  if (req.get_header_value_count("Cookie") != 1)
    return {};
  const auto prefix = QString::fromLatin1(Cookie) + '=';
  std::string result;
  for (const auto &part : QString::fromStdString(req.get_header_value("Cookie")).split(';')) {
    const auto entry = part.trimmed();
    if (entry.startsWith(prefix)) {
      if (!result.empty() || entry.size() != prefix.size() + 64)
        return {};
      result = entry.mid(prefix.size()).toStdString();
    }
  }
  return result;
}

bool jsonContent(const Request &req) {
  return req.get_header_value_count("Content-Type") == 1 &&
         QString::fromStdString(req.get_header_value("Content-Type")).section(';', 0, 0)
                 .trimmed().compare("application/json", Qt::CaseInsensitive) == 0;
}

Json parseBody(const Request &req) {
  auto json = parseBoundedJson(req.body);
  if (!json.is_object())
    throw Error("invalid_argument", "A JSON object is required.").withReason("admin_request_invalid");
  return json;
}
} // namespace

struct AdminServer::Impl {
  Service &service;
  ClientRegistry &clients;
  Json business;
  std::string mcpUrl;
  AdminConfig config;
  AdminSessions sessions;
  httplib::Server server;
  std::thread thread;

  Impl(Service &instance, const Config &runtime, ClientRegistry &registry)
      : service(instance), clients(registry), business(runtime.value.at("business")),
        config(adminConfiguration(runtime)), sessions(config) {
    config.token.clear();
    if (!config.enabled)
      return;
    server.set_socket_options(listenerSocketOptions);
    server.new_task_queue = [] { return new httplib::ThreadPool(4, 4, 16); };
    server.set_read_timeout(2).set_write_timeout(2).set_keep_alive_timeout(1)
        .set_payload_max_length(64 * 1024).set_compress(false);
    server.set_default_headers({
        {"Cache-Control", "no-store"},
        {"X-Content-Type-Options", "nosniff"},
        {"Referrer-Policy", "no-referrer"},
        {"Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"},
    });
    server.set_pre_routing_handler([this](const Request &req, Response &res) {
      if (req.get_header_value_count("Host") != 1 ||
          !allowed(config.hosts, req.get_header_value("Host"), true)) {
        sendJson(res, errorPayload(Error("forbidden", "Host is not allowed.", 403)
                                      .withReason("admin_host_rejected")), 403);
        return httplib::Server::HandlerResponse::Handled;
      }
      if (req.get_header_value_count("Origin") > 1 ||
          (req.has_header("Origin") && !allowed(config.origins, req.get_header_value("Origin")))) {
        sendJson(res, errorPayload(Error("forbidden", "Origin is not allowed.", 403)
                                      .withReason("admin_origin_rejected")), 403);
        return httplib::Server::HandlerResponse::Handled;
      }
      return httplib::Server::HandlerResponse::Unhandled;
    });
    server.set_error_handler([](const Request &, Response &res) {
      if (!res.body.empty())
        return;
      if (res.status == 413)
        sendJson(res, errorPayload(Error("payload_too_large", "Request exceeds 64 KiB.", 413)), 413);
      else if (res.status == 404)
        sendJson(res, errorPayload(Error("not_found", "Administration endpoint was not found.", 404)
                                      .withReason("admin_endpoint_not_found")), 404);
    });
    mountSettingsAssets(server);
    bind("POST", "/login-links", [this](const auto &req, auto &res) {
      if (req.has_header("Origin") || req.has_header("Cookie"))
        throw Error("forbidden", "Use the native client to request a login link.", 403)
            .withReason("admin_native_login_required");
      if (req.get_header_value_count("Authorization") != 1)
        throw Error("unauthorized", "An administration credential is required.", 401)
            .withReason("admin_credential_invalid");
      const auto code = sessions.issue(req.get_header_value("Authorization"));
      sendJson(res, {{"url", config.origin + "/settings/#login=" + code}, {"expires_in", 120}});
    });
    bind("POST", "/session", [this](const auto &req, auto &res) {
      verifyWrite(req);
      std::string cookie;
      const auto value = sessions.login(parseBody(req), cookie);
      res.set_header("Set-Cookie", buildCookie(cookie));
      sendJson(res, value);
    });
    bind("GET", "/session", [this](const auto &req, auto &res) {
      sendJson(res, sessions.require(requestCookie(req)));
    });
    bind("DELETE", "/session", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      sessions.logout(requestCookie(req));
      res.set_header("Set-Cookie", buildCookie("") + "; Max-Age=0");
      sendJson(res, {{"ok", true}});
    });
    bind("GET", "/settings", [this](const auto &req, auto &res) {
      (void)sessions.require(requestCookie(req));
      sendJson(res, service.settings().view());
    });
    bind("PUT", "/settings", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      const auto body = parseBody(req);
      try {
        validateSchema(readSchema("settings-update"), body);
      } catch (const Error &error) {
        if (error.code != "invalid_argument")
          throw;
        throw error.withMessage("Invalid settings change.").withReason("settings_patch_invalid");
      }
      sendJson(res, service.settings().update(body["patch"], body["revision"]));
    });
    bind("PUT", R"(/providers/([a-z]+)/key)", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      const auto body = parseBody(req);
      if (body.size() != 1 || !body.contains("key") || !body["key"].is_string())
        throw Error("invalid_argument", "A provider key is required.").withReason("provider_key_invalid");
      sendJson(res, service.settings().setKey(req.matches[1].str(), body["key"]));
    });
    bind("DELETE", R"(/providers/([a-z]+)/key)", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      sendJson(res, service.settings().clearKey(req.matches[1].str()));
    });
    bind("POST", R"(/providers/([a-z]+)/test)", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      sendJson(res, service.testProvider(req.matches[1].str()));
    });
    bind("GET", "/status", [this](const auto &req, auto &res) {
      (void)sessions.require(requestCookie(req));
      const auto error = service.settings().error();
      sendJson(res, {{"version", QCWS_VERSION}, {"listeners", service.listeningAddresses()}, {"mcp_url", mcpUrl},
                     {"cooldown", service.providerStatus()},
                     {"settings_error", error.empty() ? Json(nullptr) : Json(error)},
                     {"settings_error_detail", service.settings().errorDetails()}});
    });
    bind("GET", "/clients", [this](const auto &req, auto &res) {
      (void)sessions.require(requestCookie(req));
      sendJson(res, clients.view());
    });
    bind("POST", "/clients", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      const auto body = clientBody(req, true, true);
      sendJson(res, clients.create(body.at("id"), QByteArray::fromStdString(body.at("token")), body.at("revision")));
    });
    bind("POST", "/clients/reveal", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      const auto body = clientBody(req, true, false);
      sendJson(res, clients.reveal(body.at("id"), body.at("revision")));
    });
    bind("PUT", "/clients", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      const auto body = clientBody(req, true, true);
      sendJson(res, clients.replace(body.at("id"), QByteArray::fromStdString(body.at("token")),
                                   body.at("revision")));
    });
    bind("DELETE", "/clients", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      const auto body = clientBody(req, true, false);
      sendJson(res, clients.remove(body.at("id"), body.at("revision")));
    });
    bind("PUT", R"(/clients/([A-Za-z0-9_.-]{1,64}))", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      const auto body = clientBody(req, false, true);
      sendJson(res, clients.replace(req.matches[1].str(), QByteArray::fromStdString(body.at("token")),
                                   body.at("revision")));
    });
    bind("DELETE", R"(/clients/([A-Za-z0-9_.-]{1,64}))", [this](const auto &req, auto &res) {
      verifySessionWrite(req);
      const auto body = clientBody(req, false, false);
      sendJson(res, clients.remove(req.matches[1].str(), body.at("revision")));
    });
  }

  static Json clientBody(const Request &req, bool withId, bool withToken) {
    auto body = parseBody(req);
    const auto expectedFields = size_t{1} + static_cast<size_t>(withId) + static_cast<size_t>(withToken);
    if (body.size() != expectedFields ||
        !body.contains("revision") || !body["revision"].is_string() ||
        body["revision"].get_ref<const std::string &>().size() != 64 ||
        (withId && (!body.contains("id") || !body["id"].is_string())) ||
        (withToken && (!body.contains("token") || !body["token"].is_string())))
      throw Error("invalid_argument", "Invalid client request.").withReason("admin_request_invalid");
    return body;
  }

  std::string buildCookie(const std::string &value) const {
    return std::string(Cookie) + "=" + value + "; Path=/admin/api/v1; HttpOnly; SameSite=Strict" +
           (config.origin.starts_with("https://") ? "; Secure" : "");
  }
  void verifyWrite(const Request &req) const {
    if (req.get_header_value_count("Origin") != 1)
      throw Error("forbidden", "A same-origin write is required.", 403).withReason("admin_origin_rejected");
    if (!jsonContent(req))
      throw Error("invalid_argument", "Content-Type application/json is required.", 415)
          .withReason("admin_content_type_invalid");
  }
  void verifySessionWrite(const Request &req) {
    verifyWrite(req);
    if (req.get_header_value_count("X-QCWS-CSRF") != 1)
      throw Error("forbidden", "A CSRF token is required.", 403).withReason("admin_csrf_rejected");
    const auto csrf = req.get_header_value("X-QCWS-CSRF");
    (void)sessions.require(requestCookie(req), &csrf);
  }
  void bind(const std::string &method, const std::string &suffix, httplib::Server::Handler handler) {
    const auto path = Api + suffix;
    auto guarded = [handler = std::move(handler)](const Request &req, Response &res) {
      try {
        handler(req, res);
      } catch (const Error &error) {
        // Schema diagnostics can contain caller-supplied strings, including secrets.
        sendJson(res, errorPayload(error.code == "invalid_argument" ? error.withMessage("Invalid request.") : error),
                 error.status);
      } catch (const std::exception &) {
        sendJson(res, errorPayload(Error("internal_error", "Administration request failed.", 500)
                                      .withReason("admin_internal_error")), 500);
      }
    };
    if (method == "GET")
      server.Get(path, guarded);
    else if (method == "POST")
      server.Post(path, guarded);
    else if (method == "PUT")
      server.Put(path, guarded);
    else
      server.Delete(path, guarded);
  }
  void start(int businessPort, std::function<void()> onExit) {
    if (!config.enabled)
      return;
    mcpUrl = businessMcpUrl(business, businessPort);
    if (!server.bind_to_port(config.host, config.port))
      throw Error("invalid_configuration", "Cannot bind administration listener.");
    thread = std::thread([this, onExit = std::move(onExit)] { runListener(server, onExit); });
    server.wait_until_ready();
    if (!server.is_running())
      throw Error("service_error", "Administration listener stopped during startup.", 503);
  }
  void stop() {
    server.stop();
    if (thread.joinable())
      thread.join();
  }
};

AdminServer::AdminServer(Service &service, const Config &runtime, ClientRegistry &clients)
    : impl_(std::make_unique<Impl>(service, runtime, clients)) {}
AdminServer::~AdminServer() {
  stop();
}
void AdminServer::start(int businessPort, std::function<void()> onExit) {
  impl_->start(businessPort, std::move(onExit));
}
void AdminServer::stop() {
  impl_->stop();
}
} // namespace qws
