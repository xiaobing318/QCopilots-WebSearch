#pragma once
#include <functional>
#include <qws/service.hpp>

namespace qws {
class ClientRegistry;
class AdminServer {
public:
  AdminServer(Service &, const Config &, ClientRegistry &);
  ~AdminServer();
  void start(int businessPort, std::function<void()> onExit = {});
  void stop();

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
std::string settingsUrl(const Config &);
void openSettingsUrl(const std::string &);
} // namespace qws
