#include "test_support.hpp"
#include <QCoreApplication>
#include <QFile>
#include <QProcess>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTemporaryDir>
#include <atomic>
#include <future>
#include <httplib.h>
#include <iostream>
#include <qws/server.hpp>
using namespace qws;
namespace {
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
  write(*socket, headers("POST", "/search", framing) + prefix);
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
void triggerBusinessFault(int port, const httplib::Headers &auth) {
  httplib::Client business("127.0.0.1", port);
  business.set_connection_timeout(1);
  business.set_read_timeout(2);
  require(!business.Get("/mcp", auth), "failed accept queue delivered an HTTP response");
}

void verifyListenerFailure(Json value, const httplib::Headers &auth, const QString &probePath) {
  QTemporaryDir directory;
  require(directory.isValid(), "listener failure temporary directory");
  value["settings"]["directory"] = "state";
  value["cache"]["directory"] = "cache";
  value["admin"] = {{"enabled", false}};
  {
    Config config{value, directory.path().toStdString()};
    Service service(config);
    HttpServer server(service, config);
    failBusinessAcceptForTest(server);
    server.start();
    httplib::Client control("127.0.0.1", server.controlPort());
    control.set_connection_timeout(1);
    control.set_read_timeout(2);
    auto ready = control.Get("/health/ready", auth);
    require(ready && ready->status == 200 && Json::parse(ready->body).at("service") == "ready",
            "both listeners must start before the test fault");
    require(!server.listenerFailed(), "healthy listeners reported failure");

    triggerBusinessFault(server.businessPort(), auth);
    require(eventually([&] { return server.listenerFailed() && service.status().at("service") == "stopping"; }),
            "unexpected accept-loop exit must notify the process and stop the service");
    auto unavailable = control.Get("/health/ready", auth);
    require(unavailable && unavailable->status == 503 && Json::parse(unavailable->body).at("service") == "stopping",
            "the surviving control listener must withdraw readiness");
    server.stop();
    require(server.listenerFailed(), "coordinated cleanup masked the original listener failure");
  }

  // The probe runs the production CLI entry point. Use fresh ports so a
  // previous control connection in TIME_WAIT cannot prevent its bind.
  QTcpServer businessPort, controlPort;
  require(businessPort.listen(QHostAddress::LocalHost) && controlPort.listen(QHostAddress::LocalHost),
          "reserve private loopback ports for the listener failure probe");
  value["business"]["port"] = businessPort.serverPort();
  value["control"]["port"] = controlPort.serverPort();
  QFile runtime(directory.filePath("runtime.json"));
  const auto contents = QByteArray::fromStdString(value.dump());
  require(runtime.open(QIODevice::WriteOnly) && runtime.write(contents) == contents.size(),
          "write listener failure probe configuration");
  runtime.close();
  QProcess child;
  child.setReadChannel(QProcess::StandardError);
  businessPort.close();
  controlPort.close();
  child.start(probePath, {"--ConfigPath", runtime.fileName()});
  require(child.waitForStarted(5000), "could not start the listener failure CLI probe");
  QByteArray diagnostics;
  require(eventually([&] {
    if (!child.bytesAvailable())
      child.waitForReadyRead(100);
    diagnostics += child.readAllStandardError();
    return diagnostics.contains(" ready.");
  }, 5000), "the CLI never announced successful startup before the fault");
  triggerBusinessFault(value.at("business").at("port").get<int>(), auth);
  require(child.state() == QProcess::NotRunning || child.waitForFinished(10000),
          "listener failure did not exit the CLI");
  diagnostics += child.readAllStandardError();
  require(child.exitStatus() == QProcess::NormalExit && child.exitCode() == 1 &&
              diagnostics.contains("\nlistener_failed:") && child.readAllStandardOutput().isEmpty(),
          "the production CLI must report the listener fault after startup and exit with status 1");
  std::cout << "PASS unexpected business listener exit withdraws readiness and terminates the CLI\n";
}

void tests(const char *templatePath, const QString &probePath) {
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
    auto healthy = control.Get("/health/live", auth);
    require(healthy && healthy->status == 200, "health after excessive JSON nesting");
  }
  auto initialized = client.Post("/mcp", mcpHeaders, mcpInitialize(1, "2025-06-18").dump(), "application/json");
  require(initialized && initialized->status == 200, "initialize header test session");
  const auto sessionId = initialized->get_header_value("Mcp-Session-Id");
  require(!sessionId.empty(), "initialize assigned a session");
  mcpHeaders.emplace("Mcp-Session-Id", sessionId);
  for (const int scenario : {0, 1, 2, 3}) {
    auto invalid = mcpHeaders;
    // 2025-03-26 predates the header and is not a negotiated revision here.
    invalid.emplace("MCP-Protocol-Version", scenario == 0   ? "invalid"
                                            : scenario == 3 ? "2025-03-26"
                                                            : "2025-06-18");
    if (scenario == 1 || scenario == 2)
      invalid.emplace(scenario == 2 ? "Mcp-Session-Id" : "MCP-Protocol-Version", "2025-06-18");
    auto rejected = client.Delete("/mcp", invalid);
    require(rejected && rejected->status == 400, "invalid DELETE headers must not retire session");
    auto ping = client.Post("/mcp", mcpHeaders,
                            Json{{"jsonrpc", "2.0"}, {"id", scenario + 2}, {"method", "ping"}}.dump(),
                            "application/json");
    require(ping && ping->status == 200 && Json::parse(ping->body).contains("result"),
            "session survives rejected DELETE");
  }
  mcpHeaders.emplace("MCP-Protocol-Version", "2025-06-18");
  auto ready = client.Post("/mcp", mcpHeaders, R"({"jsonrpc":"2.0","method":"notifications/initialized"})",
                           "application/json");
  require(ready && ready->status == 202 && ready->body.empty(), "ready for tools/call");
  int requestId = 5;
  const auto searchBody = [&](const std::string &query) {
    return mcpToolCall(++requestId, "web_search", {{"query", query}}).dump();
  };
  const auto sessionFields = "Accept: application/json, text/event-stream\r\nMCP-Protocol-Version: 2025-06-18\r\n"
                             "Mcp-Session-Id: " + sessionId + "\r\n";
  std::vector<std::unique_ptr<QTcpSocket>> active;
  for (int i = 0; i < 5; ++i) {
    auto sock = connection(server.businessPort());
    const auto body = searchBody("barrier-" + std::to_string(i));
    write(*sock, headers("POST", "/mcp", sessionFields + "Content-Length: " + std::to_string(body.size()) + "\r\n") +
                     body);
    active.push_back(std::move(sock));
  }
  require(eventually([&] { return held == 5 && service.status()["active_calls"] == 5; }),
          "five real HTTP requests");
  auto started = Clock::now();
  auto response = client.Post("/mcp", mcpHeaders, searchBody("sixth"), "application/json");
  require(response && response->status == 200, "sixth call returns a tool result");
  const auto busy = Json::parse(response->body).at("result");
  require(busy.at("isError") == true && busy.at("structuredContent").at("error").at("code") == "busy",
          "sixth call must report exhausted capacity");
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
  response = client.Post("/mcp", mcpHeaders, searchBody("recovered"), "application/json");
  require(response && response->status == 200 && Json::parse(response->body).at("result").at("isError") == false,
          "request after cancellation");
  std::cout << "PASS actual HTTP capacity, independent health and cancellation\n";
  for (const auto *framing : {"Content-Length: 1048576\r\n", "Transfer-Encoding: chunked\r\n"}) {
    auto sock = connection(server.controlPort());
    started = Clock::now();
    write(*sock, headers("GET", "/health/live", framing));
    require(sock->waitForReadyRead(500), "control rejects framed body immediately");
    require(sock->readAll().startsWith("HTTP/1.1 400"), "control body response");
    require(Clock::now() - started < std::chrono::milliseconds(200), "control body latency");
  }
  // Exercise the shared HTTP library's input deadline at the local fixture.
  drip(upstreamPort, "Content-Length: 1048576\r\n", "{", ' ');
  drip(upstreamPort, "Transfer-Encoding: chunked\r\n", "100000;", 'x');
  drip(upstreamPort, "Transfer-Encoding: chunked\r\n", "1\r\n{\r\n0\r\nX-Slow:", 'x');
  std::cout << "PASS absolute input deadline including chunks and trailers\n";
  started = Clock::now();
  response = client.Post("/mcp", mcpHeaders, searchBody("slow-valid"), "application/json");
  require(response && response->status == 200 && Json::parse(response->body).at("result").at("isError") == false,
          "complete body must clear input deadline");
  require(Clock::now() - started >= std::chrono::milliseconds(2300), "slow handler executed");
  auto deleted = client.Delete("/mcp", mcpHeaders);
  require(deleted && deleted->status == 204, "valid session deletion with a supported protocol header");
  std::cout << "PASS bounded JSON and DELETE protocol headers\n";
  std::vector<std::unique_ptr<QTcpSocket>> pending;
  // Exceed the nine workers without overflowing the 16-slot queue before they dequeue.
  for (int i = 0; i < 12; ++i) {
    pending.push_back(connection(server.businessPort()));
  }
  started = Clock::now();
  server.stop();
  server.stop();
  require(!server.listenerFailed(), "orderly shutdown must not report a listener failure");
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
  verifyListenerFailure(value, auth, probePath);
  qunsetenv("QCWS_HTTP_TEST_TOKEN");
}
} // namespace
int main(int argc, char **argv) {
  QCoreApplication application(argc, argv);
  try {
    require(argc == 3, "template and listener failure probe arguments");
    tests(argv[1], QCoreApplication::arguments().at(2));
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
