#pragma once
#include <qws/service.hpp>

namespace qws {
class AdminServer {
public:
  AdminServer(Service &, const Config &);
  ~AdminServer();
  void start();
  void stop();

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
std::string settingsUrl(const Config &);
void openSettingsUrl(const std::string &);
} // namespace qws
