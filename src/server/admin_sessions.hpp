#pragma once
#include <deque>
#include <map>
#include <qws/common.hpp>
#include <qws/settings.hpp>

namespace qws {
class AdminSessions {
public:
  explicit AdminSessions(const AdminConfig &config);
  std::string issue(const std::string &authorization);
  Json login(const Json &body, std::string &cookie);
  Json require(const std::string &cookie, const std::string *csrf = nullptr);
  void logout(const std::string &cookie);

private:
  struct Session {
    std::string csrf;
    Clock::time_point created, touched;
  };
  std::mutex mutex_;
  std::map<QByteArray, Clock::time_point> codes_;
  std::map<QByteArray, Session> sessions_;
  std::deque<Clock::time_point> failures_;
  std::vector<ClientIdentity> credential_;
  std::chrono::minutes idle_;
  std::chrono::hours lifetime_;
  void expire(Clock::time_point now);
};
} // namespace qws
