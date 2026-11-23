#pragma once
#include <qws/protocol.hpp>
namespace qws {
// Loads the configured TLS certificate and key without binding any listener.
void checkBusinessTls(const Config &);
class HttpServer {
public:
  HttpServer(Service &, Config);
  ~HttpServer();
  void start();
  void stop();
  bool listenerFailed() const;
  int businessPort() const;
  int controlPort() const;

private:
  friend void failBusinessAcceptForTest(HttpServer &);
  struct Impl;
  std::unique_ptr<Impl> d_;
};
int runStdio(Service &);
} // namespace qws
