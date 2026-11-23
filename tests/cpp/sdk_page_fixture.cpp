#include <QCoreApplication>
#include <QTemporaryDir>
#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <qws/server.hpp>
#include <thread>

namespace {
std::atomic_bool stopped = false;
static_assert(std::atomic_bool::is_always_lock_free);
void stop(int) {
  stopped = true;
}

qws::HttpResponse fixedPage(std::string url, std::shared_ptr<qws::CancelToken>) {
  if (url != "https://example.org/fixture/0")
    throw qws::Error("unexpected_fixture_url", "Unexpected test page URL.");
  return {200, "First fixture line\nNeedle on the second line\nLast fixture line",
          "text/plain; charset=utf-8", std::move(url), {}};
}
} // namespace

int main(int argc, char **argv) {
  QCoreApplication app(argc, argv);
  const auto args = app.arguments();
  const bool stdio = args.size() == 4 && args.at(3) == "--Stdio";
  if ((!stdio && args.size() != 3) || args.at(1) != "--ConfigPath")
    return 2;
  try {
    auto config = qws::Config::load(args.at(2).toStdString());
    QTemporaryDir cache;
    if (!cache.isValid())
      return 1;
    config.value["cache"]["directory"] = cache.path().toStdString();
    qws::ServiceTransport transport;
    transport.open = fixedPage;
    qws::Service service(config, std::move(transport));
    if (stdio)
      return qws::runStdio(service);
    std::signal(SIGTERM, stop);
    std::signal(SIGINT, stop);
#ifdef _WIN32
    std::signal(SIGBREAK, stop);
#endif
    qws::HttpServer server(service, config);
    server.start();
    while (!stopped)
      std::this_thread::sleep_for(std::chrono::milliseconds(25));
    server.stop();
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
