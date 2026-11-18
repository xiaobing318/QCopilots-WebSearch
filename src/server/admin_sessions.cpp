#include "admin_sessions.hpp"
#include <QCryptographicHash>

namespace qws {
namespace {
QByteArray digest(const std::string &value) {
  return QCryptographicHash::hash(QByteArray::fromStdString(value), QCryptographicHash::Sha256);
}
} // namespace
AdminSessions::AdminSessions(const AdminConfig &config)
    : credential_{{"admin", digest(config.token)}}, idle_(config.sessionIdleMinutes),
      lifetime_(config.sessionMaxHours) {}

void AdminSessions::expire(Clock::time_point now) {
  std::erase_if(codes_, [&](const auto &entry) { return entry.second <= now; });
  std::erase_if(sessions_, [&](const auto &entry) {
    const auto &session = entry.second;
    return now - session.touched >= idle_ || now - session.created >= lifetime_;
  });
  while (!failures_.empty() && now - failures_.front() >= std::chrono::minutes(10))
    failures_.pop_front();
}

std::string AdminSessions::issue(const std::string &authorization) {
  std::lock_guard guard(mutex_);
  const auto now = Clock::now();
  expire(now);
  if (failures_.size() >= 10)
    throw Error("rate_limited", "Too many login failures.", 429, true);
  try {
    (void)authenticate(credential_, authorization);
  } catch (const Error &) {
    failures_.push_back(now);
    throw;
  }
  if (codes_.size() >= 8)
    throw Error("rate_limited", "Too many pending login links.", 429, true);
  const auto code = generateToken().toStdString();
  codes_.emplace(digest(code), now + std::chrono::seconds(120));
  return code;
}

Json AdminSessions::login(const Json &body, std::string &cookie) {
  const bool code = body.is_object() && body.size() == 1 && body.contains("code") && body["code"].is_string();
  const bool token = body.is_object() && body.size() == 1 && body.contains("token") && body["token"].is_string();
  if (!code && !token)
    throw Error("invalid_argument", "A login code or token is required.");
  std::lock_guard guard(mutex_);
  const auto now = Clock::now();
  expire(now);
  if (failures_.size() >= 10)
    throw Error("rate_limited", "Too many login failures.", 429, true);
  try {
    if (code) {
      const auto &value = body["code"].get_ref<const std::string &>();
      if (value.size() != 64 || !codes_.erase(digest(value)))
        throw Error("unauthorized", "Invalid or expired login code.", 401);
    } else {
      (void)authenticate(credential_, "Bearer " + body["token"].get<std::string>());
    }
  } catch (const Error &) {
    failures_.push_back(now);
    throw;
  }
  if (sessions_.size() == 16) {
    const auto oldest = std::min_element(sessions_.begin(), sessions_.end(),
        [](const auto &lhs, const auto &rhs) { return lhs.second.touched < rhs.second.touched; });
    sessions_.erase(oldest);
  }
  cookie = generateToken().toStdString();
  const auto csrf = generateToken().toStdString();
  sessions_.emplace(digest(cookie), Session{csrf, now, now});
  return {{"csrf", csrf}};
}

Json AdminSessions::require(const std::string &cookie, const std::string *csrf) {
  std::lock_guard guard(mutex_);
  const auto now = Clock::now();
  expire(now);
  const auto session = cookie.size() == 64 ? sessions_.find(digest(cookie)) : sessions_.end();
  if (session == sessions_.end())
    throw Error("unauthorized", "An administration session is required.", 401);
  if (csrf && *csrf != session->second.csrf)
    throw Error("forbidden", "The CSRF token is invalid.", 403);
  session->second.touched = now;
  return {{"csrf", session->second.csrf}};
}
void AdminSessions::logout(const std::string &cookie) {
  std::lock_guard guard(mutex_);
  sessions_.erase(digest(cookie));
}
} // namespace qws
