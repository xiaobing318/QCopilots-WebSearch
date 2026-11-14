#include <QUuid>
#include <algorithm>
#include <qws/protocol.hpp>
namespace qws {
Protocol::Protocol(Service &s, std::string version, Now now, TransportPolicy transport)
    : service_(s), version_(std::move(version)), now_(std::move(now)), transport_(transport) {
  if (!now_)
    now_ = [] { return Clock::now(); };
}
bool Protocol::validId(const Json &id) {
  return id.is_string() || id.is_number_integer() || id.is_number_unsigned();
}
std::string Protocol::idKey(const Json &id) {
  return std::string(id.is_string() ? "s:" : "n:") + id.dump();
}
std::optional<std::string> Protocol::validateJsonRpc(const Json &m) {
  if (!m.is_object() || !m.contains("jsonrpc") || m["jsonrpc"] != "2.0")
    return "A single JSON-RPC 2.0 object is required";
  const bool method = m.contains("method"), result = m.contains("result"), error = m.contains("error"),
             id = m.contains("id");
  if (id && !validId(m["id"]))
    return "Request id must be a string or integer";
  if (method) {
    if (!m["method"].is_string() || result || error)
      return "Invalid JSON-RPC request";
    if (m.contains("params") && !m["params"].is_object())
      return "MCP params must be an object";
    return std::nullopt;
  }
  if (!id || result == error || m.contains("params"))
    return "Invalid JSON-RPC response";
  if (result && !m["result"].is_object())
    return "MCP result must be an object";
  if (error) {
    const auto &e = m["error"];
    if (!e.is_object() || !e.contains("code") || !e["code"].is_number_integer() || !e.contains("message") ||
        !e["message"].is_string())
      return "Invalid JSON-RPC error";
  }
  return std::nullopt;
}
ProtocolReply Protocol::rpcError(int status, const Json &id, int code, const std::string &message,
                                 const std::string &sid) {
  return {status, Json{{"jsonrpc", "2.0"}, {"id", id}, {"error", {{"code", code}, {"message", message}}}},
          sid};
}
Json Protocol::toolResult(Json payload, bool error) {
  auto text = payload.dump();
  return {{"content", Json::array({Json{{"type", "text"}, {"text", text}}})},
          {"structuredContent", std::move(payload)},
          {"isError", error}};
}
void Protocol::retireLocked(Session &s) {
  for (auto &[key, t] : s.inFlight) {
    (void)key;
    t->cancelled = true;
  }
  s.usedIds.clear();
  s.recentIds.clear();
  s.usedIdBytes = 0;
}
void Protocol::rememberIdLocked(Session &s, const std::string &key) {
  // Senders must use session-unique IDs. This receiver window bounds memory,
  // while the independent inFlight map always protects active request IDs.
  if (transport_ == TransportPolicy::Stdio) {
    while (!s.recentIds.empty() && (s.usedIds.size() >= 4096 || key.size() > 1024 * 1024 - s.usedIdBytes)) {
      s.usedIdBytes -= s.recentIds.front().size();
      s.usedIds.erase(s.recentIds.front());
      s.recentIds.pop_front();
    }
    s.recentIds.push_back(key);
  }
  s.usedIds.insert(key);
  s.usedIdBytes += key.size();
}
void Protocol::expireLocked(Clock::time_point now) {
  if (transport_ == TransportPolicy::Stdio)
    return;
  std::erase_if(sessions_, [&](const auto &entry) {
    if (entry.second->expires <= now && entry.second->inFlight.empty()) {
      retireLocked(*entry.second);
      return true;
    }
    return false;
  });
}
ProtocolReply Protocol::finishCall(const std::shared_ptr<Session> &s, const std::string &sid,
                                   const std::string &key, const std::shared_ptr<CancelToken> &t,
                                   ProtocolReply reply) {
  std::lock_guard lock(mutex_);
  if (auto it = s->inFlight.find(key); it != s->inFlight.end() && it->second == t)
    s->inFlight.erase(it);
  s->expires = now_() + std::chrono::minutes(30);
  if (t->cancelled)
    return {202, std::nullopt, sid};
  return reply;
}
ProtocolReply Protocol::handle(const std::string &principal, const std::string &sid, const Json &m,
                               std::shared_ptr<CancelToken> cancellation) {
  if (principal.empty())
    return {401, std::nullopt, {}};
  if (auto error = validateJsonRpc(m)) {
    const Json id = m.is_object() && m.contains("id") && validId(m["id"]) ? m["id"] : Json(nullptr);
    return rpcError(400, id, -32600, *error);
  }
  const bool hasMethod = m.contains("method"), hasId = m.contains("id"), notification = hasMethod && !hasId;
  const std::string method = hasMethod ? m["method"].get<std::string>() : "";
  const Json id = hasId ? m["id"] : Json(nullptr), params = m.value("params", Json::object());
  const auto now = now_();
  if (method == "initialize") {
    if (!hasId)
      return {400, std::nullopt, {}};
    if (!params.contains("protocolVersion") || !params["protocolVersion"].is_string() ||
        params["protocolVersion"] == "" || !params.contains("capabilities") ||
        !params["capabilities"].is_object() || !params.contains("clientInfo") ||
        !params["clientInfo"].is_object() || !params["clientInfo"].contains("name") ||
        !params["clientInfo"]["name"].is_string() || !params["clientInfo"].contains("version") ||
        !params["clientInfo"]["version"].is_string())
      return rpcError(200, id, -32602, "Invalid initialize parameters");
    std::lock_guard lock(mutex_);
    expireLocked(now);
    if (closing_)
      return {503, std::nullopt, {}};
    if (!sid.empty()) {
      auto it = sessions_.find(sid);
      if (it == sessions_.end() || it->second->principal != principal)
        return {404, std::nullopt, {}};
      return rpcError(200, id, -32600, "Initialize requires a new session");
    }
    const auto owned = std::count_if(sessions_.begin(), sessions_.end(),
                                     [&](const auto &s) { return s.second->principal == principal; });
    if (sessions_.size() >= 100 || owned >= 20) {
      auto oldest = sessions_.end();
      for (auto it = sessions_.begin(); it != sessions_.end(); ++it) {
        if (it->second->principal != principal || !it->second->inFlight.empty())
          continue;
        if (oldest == sessions_.end() || it->second->expires < oldest->second->expires)
          oldest = it;
      }
      if (oldest == sessions_.end())
        return rpcError(503, id, -32000, "Session capacity exhausted");
      retireLocked(*oldest->second);
      sessions_.erase(oldest);
    }
    const auto key = idKey(id);
    if (key.size() > 1024 * 1024)
      return rpcError(400, id, -32600, "Request id exceeds session budget");
    std::string fresh;
    do {
      fresh = QUuid::createUuid().toString(QUuid::WithoutBraces).toStdString();
    } while (sessions_.contains(fresh));
    auto s = std::make_shared<Session>();
    s->principal = principal;
    s->expires = now + std::chrono::minutes(30);
    rememberIdLocked(*s, key);
    sessions_.emplace(fresh, std::move(s));
    return {200,
            Json{{"jsonrpc", "2.0"},
                 {"id", id},
                 {"result",
                  {{"protocolVersion", "2025-06-18"},
                   {"capabilities", {{"tools", Json::object()}}},
                   {"serverInfo", {{"name", "qcopilots-websearch"}, {"version", version_}}}}}},
            fresh};
  }
  std::shared_ptr<Session> session;
  std::shared_ptr<CancelToken> token;
  const std::string key = hasId ? idKey(id) : "";
  std::string tool;
  Json args = Json::object();
  {
    std::lock_guard lock(mutex_);
    expireLocked(now);
    if (closing_)
      return {503, std::nullopt, {}};
    if (sid.empty())
      return {400, std::nullopt, {}};
    auto it = sessions_.find(sid);
    if (it == sessions_.end() || it->second->principal != principal)
      return {404, std::nullopt, {}};
    session = it->second;
    session->expires = now + std::chrono::minutes(30);
    if (notification) {
      if (method == "notifications/initialized")
        session->ready = true;
      else if (method == "notifications/cancelled" && params.contains("requestId") &&
               validId(params["requestId"]))
        if (auto pending = session->inFlight.find(idKey(params["requestId"]));
            pending != session->inFlight.end())
          pending->second->cancelled = true;
      return {202, std::nullopt, sid};
    }
    if (!hasMethod)
      return {202, std::nullopt, sid};
    if (session->inFlight.contains(key) || session->usedIds.contains(key))
      return rpcError(200, id, -32600, "Request id has already been used", sid);
    if (transport_ == TransportPolicy::Stdio && key.size() > 1024 * 1024)
      return rpcError(200, id, -32600, "Request id exceeds session budget", sid);
    if (transport_ == TransportPolicy::StreamableHttp &&
        (session->usedIds.size() >= 4096 || key.size() > 1024 * 1024 - session->usedIdBytes)) {
      retireLocked(*session);
      sessions_.erase(it);
      return {404, std::nullopt, {}};
    }
    rememberIdLocked(*session, key);
    if (method == "ping")
      return {200, Json{{"jsonrpc", "2.0"}, {"id", id}, {"result", Json::object()}}, sid};
    if (!session->ready)
      return rpcError(200, id, -32002, "Session has not completed initialization", sid);
    if (method != "tools/list" && method != "tools/call")
      return rpcError(200, id, -32601, "Method not found", sid);
    if (method == "tools/list") {
      if (params.contains("cursor"))
        return rpcError(200, id, -32602, "Tool list does not use cursors", sid);
    } else {
      if (!params.contains("name") || !params["name"].is_string() ||
          (params.contains("arguments") && !params["arguments"].is_object()))
        return rpcError(200, id, -32602, "Invalid tool call parameters", sid);
      tool = params["name"].get<std::string>();
      args = params.value("arguments", Json::object());
      token = cancellation ? cancellation : std::make_shared<CancelToken>();
      session->inFlight.emplace(key, token);
    }
  }
  ProtocolReply reply;
  try {
    const auto tools = service_.tools();
    if (method == "tools/list")
      reply = {200, Json{{"jsonrpc", "2.0"}, {"id", id}, {"result", {{"tools", tools}}}}, sid};
    else if (!std::any_of(tools.begin(), tools.end(), [&](const Json &d) { return d["name"] == tool; }))
      reply = rpcError(200, id, -32602, "Unknown tool", sid);
    else {
      try {
        validateSchema(readSchema(tool), args);
      } catch (const Error &) {
        return finishCall(session, sid, key, token, rpcError(200, id, -32602, "Invalid tool arguments", sid));
      }
      reply = {200,
               Json{{"jsonrpc", "2.0"},
                    {"id", id},
                    {"result", toolResult(service_.call(tool, args, principal, token), false)}},
               sid};
    }
  } catch (const Error &e) {
    if (token)
      reply = {200, Json{{"jsonrpc", "2.0"}, {"id", id}, {"result", toolResult(errorPayload(e), true)}}, sid};
    else
      reply = rpcError(200, id, -32603, "Internal server error", sid);
  } catch (...) {
    reply = rpcError(200, id, -32603, "Internal server error", sid);
  }
  return token ? finishCall(session, sid, key, token, std::move(reply)) : reply;
}
ProtocolReply Protocol::deleteSession(const std::string &principal, const std::string &sid) {
  if (principal.empty())
    return {401, std::nullopt, {}};
  if (sid.empty())
    return {400, std::nullopt, {}};
  std::lock_guard lock(mutex_);
  expireLocked(now_());
  auto it = sessions_.find(sid);
  if (it == sessions_.end() || it->second->principal != principal)
    return {404, std::nullopt, {}};
  retireLocked(*it->second);
  sessions_.erase(it);
  return {204, std::nullopt, {}};
}
void Protocol::shutdown() {
  std::lock_guard lock(mutex_);
  closing_ = true;
  for (auto &[id, s] : sessions_) {
    (void)id;
    retireLocked(*s);
  }
  sessions_.clear();
}
} // namespace qws
