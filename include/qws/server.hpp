#pragma once
#include <qws/protocol.hpp>
namespace qws {
class HttpServer {
public:
  HttpServer(Service &, Config);
  ~HttpServer();
  void start();
  void stop();
  int businessPort() const;
  int controlPort() const;

private:
  struct Impl;
  std::unique_ptr<Impl> d_;
};
int runStdio(Service &);
} // namespace qws
