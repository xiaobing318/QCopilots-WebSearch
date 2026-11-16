#include <QCoreApplication>
#include <QFile>
#include <QTcpSocket>
#include <QTemporaryDir>
#include <atomic>
#include <future>
#include <httplib.h>
#include <iostream>
#include <qws/server.hpp>
#include <stdexcept>
using namespace qws;
namespace {
void require(bool value, const char *message) {
  if (!value)
    throw std::runtime_error(message);
}
template <class F> bool eventually(F test, int milliseconds = 2000) {
  const auto deadline = Clock::now() + std::chrono::milliseconds(milliseconds);
  while (Clock::now() < deadline) {
    if (test())
      return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
  return test();
}
std::unique_ptr<QTcpSocket> connection(int port) {
  auto socket = std::make_unique<QTcpSocket>();
  socket->connectToHost("127.0.0.1", port);
  require(socket->waitForConnected(1000), "socket connect");
  return socket;
}
void write(QTcpSocket &socket, const std::string &data) {
  require(socket.write(data.data(), qint64(data.size())) == qint64(data.size()), "socket write");
  if (socket.bytesToWrite())
    require(socket.waitForBytesWritten(1000), "socket flush");
}
const std::string bearer(64, 'a');
std::string headers(const std::string &method, const std::string &path, const std::string &framing) {
  return method + " " + path + " HTTP/1.1\r\nHost: test.local\r\nAuthorization: Bearer " + bearer +
         "\r\nContent-Type: application/json\r\nConnection: close\r\n" + framing + "\r\n";
}
void drip(int port, const std::string &framing, const std::string &prefix, char byte) {
  auto socket = connection(port);
  const auto started = Clock::now();
  write(*socket, headers("POST", "/v1/search", framing) + prefix);
  auto next = Clock::now();
  while (socket->state() != QAbstractSocket::UnconnectedState &&
         Clock::now() - started < std::chrono::seconds(4)) {
    if (Clock::now() >= next) {
      socket->write(&byte, 1);
      socket->flush();
      next = Clock::now() + std::chrono::milliseconds(100);
    }
    socket->waitForReadyRead(20);
    socket->readAll();
  }
  require(socket->state() == QAbstractSocket::UnconnectedState, "slow input held connection");
  require(Clock::now() - started < std::chrono::milliseconds(2800), "absolute input deadline");
}
void tests(const char *templatePath) {
  QTemporaryDir directory;
  require(directory.isValid(), "temporary directory");
  QFile input(QString::fromUtf8(templatePath));
  require(input.open(QIODevice::ReadOnly), "configuration template");
  auto value = Json::parse(input.readAll().toStdString());
  for (const auto *name : {"business", "control"}) {
    value[name]["port"] = 0;
    value[name]["allowed_hosts"] = Json::array({"test.local"});
  }
  value["clients"] = Json::array({{{"id", "http-test"}, {"token_env", "QCWS_HTTP_TEST_TOKEN"}}});
  value["cache"]["directory"] = directory.path().toStdString();
  qputenv("QCWS_HTTP_TEST_TOKEN", QByteArray::fromStdString(bearer));
  httplib::Server upstream;
  upstream.set_read_timeout(2).set_write_timeout(2);
  std::atomic_int held{0};
  std::atomic_bool release{false};
  upstream.Post("/search", [&](const auto &request, auto &response) {
    if (request.body.find("barrier-") != std::string::npos) {
      ++held;
      while (!release && !request.is_connection_closed())
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
      --held;
    }
    if (request.body.find("slow-valid") != std::string::npos)
      std::this_thread::sleep_for(std::chrono::milliseconds(2300));
    response.set_content(R"({"results":[],"unresponsive_engines":[]})", "application/json");
  });
  const auto upstreamPort = upstream.bind_to_any_port("127.0.0.1");
  require(upstreamPort > 0, "upstream port");
  std::jthread upstreamThread([&] { upstream.listen_after_bind(); });
  upstream.wait_until_ready();
  struct Guard {
    httplib::Server &server;
    std::atomic_bool &release;
    ~Guard() {
      release = true;
      server.stop();
    }
  } upstreamGuard{upstream, release};
  value["provider"]["url"] = "http://127.0.0.1:" + std::to_string(upstreamPort) + "/search";
  Config config{value, directory.path().toStdString()};
  Service service(config);
  HttpServer server(service, config);
  server.start();
  httplib::Headers auth{{"Host", "test.local"}, {"Authorization", "Bearer " + bearer}};
  httplib::Client client("127.0.0.1", server.businessPort());
  client.set_connection_timeout(1);
  client.set_read_timeout(5);
  httplib::Client control("127.0.0.1", server.controlPort());
  control.set_connection_timeout(1);
  control.set_read_timeout(1);
  auto mcpHeaders = auth;
  mcpHeaders.emplace("Accept", "application/json, text/event-stream");
  for (const int depth : {65, 10000, 30000}) {
    const auto body = std::string(R"({"jsonrpc":"2.0","id":1,"method":"ping","params":{"deep":)") +
                      std::string(depth, '[') + "0" + std::string(depth, ']') + "}}";
    auto rejected = client.Post("/mcp", mcpHeaders, body, "application/json");
    require(rejected && rejected->status == 400, "deep MCP JSON must be rejected without crashing");
    require(Json::parse(rejected->body)["error"]["code"] == -32700, "bounded MCP parse error");
    rejected = client.Post("/v1/search", auth, body, "application/json");
    require(rejected && rejected->status == 400, "deep REST JSON must be rejected");
    auto healthy = control.Get("/health/live", auth);
    require(healthy && healthy->status == 200, "health after excessive JSON nesting");
  }
  auto initialized = client.Post(
      "/mcp", mcpHeaders,
      R"({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"headers","version":"1"}}})",
      "application/json");
  require(initialized && initialized->status == 200, "initialize header test session");
  mcpHeaders.emplace("Mcp-Session-Id", initialized->get_header_value("Mcp-Session-Id"));
  for (const int scenario : {0, 1, 2}) {
    auto invalid = mcpHeaders;
    invalid.emplace("MCP-Protocol-Version", scenario == 0 ? "invalid" : "2025-06-18");
    if (scenario != 0)
      invalid.emplace(scenario == 2 ? "Mcp-Session-Id" : "MCP-Protocol-Version", "2025-06-18");
    auto rejected = client.Delete("/mcp", invalid);
    require(rejected && rejected->status == 400, "invalid DELETE headers must not retire session");
    auto ping = client.Post("/mcp", mcpHeaders,
                            Json{{"jsonrpc", "2.0"}, {"id", scenario + 2}, {"method", "ping"}}.dump(),
                            "application/json");
    require(ping && ping->status == 200 && Json::parse(ping->body).contains("result"),
            "session survives rejected DELETE");
  }
  auto deleted = client.Delete("/mcp", mcpHeaders);
  require(deleted && deleted->status == 204, "valid session deletion");
  std::cout << "PASS bounded JSON and DELETE protocol headers\n";
  std::vector<std::unique_ptr<QTcpSocket>> active;
  for (int i = 0; i < 5; ++i) {
    auto sock = connection(server.businessPort());
    const auto body = Json{{"query", "barrier-" + std::to_string(i)}}.dump();
    write(*sock,
          headers("POST", "/v1/search", "Content-Length: " + std::to_string(body.size()) + "\r\n") + body);
    active.push_back(std::move(sock));
  }
  require(eventually([&] { return held == 5 && service.status()["active_calls"] == 5; }),
          "five real HTTP requests");
  auto started = Clock::now();
  auto response = client.Post("/v1/search", auth, R"({"query":"sixth"})", "application/json");
  require(response && response->status == 429, "sixth request must return 429");
  require(Clock::now() - started < std::chrono::milliseconds(200), "busy response latency");
  started = Clock::now();
  response = control.Get("/health/live", auth);
  require(response && response->status == 200, "health during saturation");
  require(Clock::now() - started < std::chrono::milliseconds(200), "health latency");
  for (auto &sock : active)
    sock->abort();
  require(eventually([&] {
            return held == 0 && service.status()["active_calls"] == 0 &&
                   service.status()["active_upstream"] == 0;
          }),
          "cancel restores all capacity");
  release = true;
  response = client.Post("/v1/search", auth, R"({"query":"recovered"})", "application/json");
  require(response && response->status == 200, "request after cancellation");
  std::cout << "PASS actual HTTP capacity, independent health and cancellation\n";
  for (const auto *framing : {"Content-Length: 1048576\r\n", "Transfer-Encoding: chunked\r\n"}) {
    auto sock = connection(server.controlPort());
    started = Clock::now();
    write(*sock, headers("GET", "/health/live", framing));
    require(sock->waitForReadyRead(500), "control rejects framed body immediately");
    require(sock->readAll().startsWith("HTTP/1.1 400"), "control body response");
    require(Clock::now() - started < std::chrono::milliseconds(200), "control body latency");
  }
  drip(server.businessPort(), "Content-Length: 1048576\r\n", "{", ' ');
  drip(server.businessPort(), "Transfer-Encoding: chunked\r\n", "100000;", 'x');
  drip(server.businessPort(), "Transfer-Encoding: chunked\r\n", "1\r\n{\r\n0\r\nX-Slow:", 'x');
  std::cout << "PASS absolute input deadline including chunks and trailers\n";
  started = Clock::now();
  response = client.Post("/v1/search", auth, R"({"query":"slow-valid"})", "application/json");
  require(response && response->status == 200, "complete body must clear input deadline");
  require(Clock::now() - started >= std::chrono::milliseconds(2300), "slow handler executed");
  std::vector<std::unique_ptr<QTcpSocket>> pending;
  for (int i = 0; i < 25; ++i) {
    auto sock = connection(server.businessPort());
    write(*sock, headers("POST", "/v1/search", "Content-Length: 1048576\r\n") + "{");
    pending.push_back(std::move(sock));
  }
  started = Clock::now();
  server.stop();
  server.stop();
  const auto stopped = Clock::now() - started;
  const auto stoppedMs = std::chrono::duration_cast<std::chrono::milliseconds>(stopped).count();
  if (stopped >= std::chrono::seconds(2))
    std::cerr << "active and queued socket shutdown took " << stoppedMs << " ms\n";
  require(stopped < std::chrono::seconds(2), "stop interrupts active and queued sockets");
  for (auto &sock : pending) {
    if (sock->state() != QAbstractSocket::UnconnectedState)
      sock->waitForDisconnected(1000);
    require(sock->state() == QAbstractSocket::UnconnectedState, "queued socket survived shutdown");
  }
  std::cout << "PASS active and queued sockets stop, repeated stop\n";
  {
    httplib::Server longRead;
    longRead.set_read_timeout(30).set_write_timeout(2);
    std::atomic_bool reading{false};
    longRead.set_pre_routing_handler([&](const auto &, auto &) {
      reading = true;
      return httplib::Server::HandlerResponse::Unhandled;
    });
    longRead.Post("/partial", [](const auto &, auto &res) { res.set_content("unexpected", "text/plain"); });
    const int port = longRead.bind_to_any_port("127.0.0.1");
    require(port > 0, "long-read listener port");
    std::jthread listener([&] { longRead.listen_after_bind(); });
    struct StopGuard {
      httplib::Server &server;
      ~StopGuard() { server.stop(); }
    } stopGuard{longRead};
    longRead.wait_until_ready();
    auto partial = connection(port);
    write(*partial, headers("POST", "/partial", "Content-Length: 1048576\r\n") + "{");
    require(eventually([&] { return reading.load(); }), "partial request reached body reader");
    require(!partial->waitForReadyRead(100) && partial->state() == QAbstractSocket::ConnectedState,
            "incomplete long-read request must remain pending");
    started = Clock::now();
    longRead.stop();
    listener.join();
    const auto elapsed = Clock::now() - started;
    const auto elapsedMs = std::chrono::duration_cast<std::chrono::milliseconds>(elapsed).count();
    if (elapsed >= std::chrono::seconds(2))
      std::cerr << "30-second input read shutdown took " << elapsedMs << " ms\n";
    require(elapsed < std::chrono::seconds(2), "stop must interrupt a long read before its input deadline");
    std::cout << "PASS 30-second input read interrupted in " << elapsedMs << " ms\n";
  }
  for (int iteration = 0; iteration < 3; ++iteration) {
    const int port = upstream.bind_to_any_port("127.0.0.1");
    if (iteration == 0) {
      require(port < 0, "cannot rebind active listener");
      upstream.stop();
      upstreamThread.join();
      continue;
    }
    require(port > 0, "rebind after stop and join");
    upstreamThread = std::jthread([&] { upstream.listen_after_bind(); });
    upstream.wait_until_ready();
    upstream.stop();
    upstream.stop();
    upstreamThread.join();
  }
  qunsetenv("QCWS_HTTP_TEST_TOKEN");
}
} // namespace
int main(int argc, char **argv) {
  QCoreApplication application(argc, argv);
  try {
    require(argc == 2, "template argument");
    tests(argv[1]);
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
