#include <QCoreApplication>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QProcessEnvironment>
#include <QTcpServer>
#include <QTemporaryDir>
#include <iostream>
#include <qws/config.hpp>
#include <stdexcept>
using qws::Json;
namespace {
void require(bool ok, const char *message) {
  if (!ok)
    throw std::runtime_error(message);
}
class Child {
  QProcess process;

public:
  Child(const QString &exe, const QString &config) {
    process.setProcessChannelMode(QProcess::SeparateChannels);
    auto env = QProcessEnvironment::systemEnvironment();
    env.insert("QCWS_PROCESS_TEST_TOKEN", QString(64, 's'));
    process.setProcessEnvironment(env);
    process.start(exe, {"--ConfigPath", config, "--Stdio"});
    require(process.waitForStarted(10000), "Cannot start stdio child");
  }
  ~Child() {
    if (process.state() != QProcess::NotRunning) {
      process.closeWriteChannel();
      if (!process.waitForFinished(1500)) {
        process.kill();
        process.waitForFinished(1500);
      }
    }
  }
  void bytes(const QByteArray &value) {
    require(process.write(value) == value.size(), "Cannot queue stdio message");
    QElapsedTimer timer;
    timer.start();
    while (process.bytesToWrite() > 0) {
      int remaining = 5000 - int(timer.elapsed());
      require(remaining > 0 && process.waitForBytesWritten(remaining), "Stdio write deadline");
    }
  }
  void send(const Json &message) {
    bytes(QByteArray::fromStdString(message.dump()) + '\n');
  }
  Json receive() {
    QElapsedTimer timer;
    timer.start();
    while (!process.canReadLine()) {
      int remaining = 5000 - int(timer.elapsed());
      require(process.state() != QProcess::NotRunning && remaining > 0 && process.waitForReadyRead(remaining),
              "Missing stdio response");
    }
    auto line = process.readLine();
    require(line.size() <= 1024 * 1024, "Stdio response too large");
    auto result = Json::parse(line.constData(), line.constData() + line.size(), nullptr, false);
    require(result.is_object() && result.value("jsonrpc", "") == "2.0", "stdout is not JSON-RPC");
    return result;
  }
  Json exchange(const Json &request) {
    send(request);
    auto reply = receive();
    require(reply["id"] == request["id"], "Stdio ID changed");
    return reply;
  }
  void ping(Json id) {
    require(exchange({{"jsonrpc", "2.0"}, {"id", std::move(id)}, {"method", "ping"}}).contains("result"),
            "Ping failed");
  }
  void initialize(const std::string &version = "2025-06-18") {
    auto reply = exchange({{"jsonrpc", "2.0"},
                           {"id", "init"},
                           {"method", "initialize"},
                           {"params",
                            {{"protocolVersion", version},
                             {"capabilities", Json::object()},
                             {"clientInfo", {{"name", "process-test"}, {"version", "1"}}}}}});
    require(reply.contains("result") && reply["result"]["protocolVersion"] == version, "Initialize failed");
    require(reply["result"]["instructions"].is_string(), "Initialize lacks instructions");
    send({{"jsonrpc", "2.0"}, {"method", "notifications/initialized"}});
  }
  void finish() {
    process.closeWriteChannel();
    require(process.waitForFinished(10000), "EOF did not stop stdio child");
    require(process.exitStatus() == QProcess::NormalExit && process.exitCode() == 0, "Stdio child failed");
    require(process.readAllStandardOutput().isEmpty(), "Unexpected stdout after EOF");
    require(!process.readAllStandardError().contains(QByteArray(64, 's')), "Credential leaked to stderr");
  }
};
} // namespace
int main(int argc, char **argv) {
  QCoreApplication app(argc, argv);
  try {
    require(argc == 3, "Expected executable and runtime example paths");
    QTemporaryDir temporary;
    require(temporary.isValid(), "No temporary directory");
    QFile example(QString::fromLocal8Bit(argv[2]));
    require(example.open(QIODevice::ReadOnly), "No runtime example");
    auto config = Json::parse(example.readAll().toStdString());
    QTcpServer business, control;
    require(business.listen(QHostAddress::LocalHost) && control.listen(QHostAddress::LocalHost),
            "No dynamic fixture ports");
    config["business"]["port"] = business.serverPort();
    config["control"]["port"] = control.serverPort();
    config["clients"] = Json::array({{{"id", "test"}, {"token_env", "QCWS_PROCESS_TEST_TOKEN"}}});
    config["cache"]["directory"] = temporary.filePath("unused-persistent-cache").toStdString();
    const auto path = temporary.filePath("stdio.json");
    QFile output(path);
    require(output.open(QIODevice::WriteOnly), "Cannot create configuration");
    auto data = QByteArray::fromStdString(config.dump());
    require(output.write(data) == data.size(), "Cannot save configuration");
    output.close();
    // Reserved ports remain bound throughout: stdio must never open listeners.
    Child first(QString::fromLocal8Bit(argv[1]), path), second(QString::fromLocal8Bit(argv[1]), path);
    Json oversized = {{"jsonrpc", "2.0"},
                      {"id", ""},
                      {"method", "initialize"},
                      {"params",
                       {{"protocolVersion", "2025-06-18"},
                        {"capabilities", Json::object()},
                        {"clientInfo", {{"name", "test"}, {"version", "1"}}}}}};
    oversized["id"] = std::string(1024 * 1024 - oversized.dump().size(), 'x');
    require(oversized.dump().size() == 1024 * 1024, "Exact input limit fixture");
    first.send(oversized);
    auto oversizedError = first.receive();
    require(oversizedError["id"].is_null() && oversizedError["error"]["code"] == -32600,
            "Oversized ID must be a bounded protocol rejection");
    require(
        first.exchange({{"jsonrpc", "2.0"}, {"id", "before"}, {"method", "tools/list"}}).contains("error"),
        "Pre-initialize request was lost");
    first.initialize();
    first.ping(std::string(65534, 'x'));
    second.initialize("2025-11-25");
    second.ping("alive");
    for (int i = 0; i < 5000; ++i)
      first.ping(i);
    for (int i = 0; i < 40; ++i)
      first.ping("large-" + std::to_string(i) + "-" + std::string(32768, 'x'));
    first.ping("after-history");
    first.ping("duplicate");
    require(first.exchange({{"jsonrpc", "2.0"}, {"id", "duplicate"}, {"method", "ping"}}).contains("error"),
            "Recent duplicate accepted");
    first.bytes("{invalid-json\n");
    auto error = first.receive();
    require(error["id"].is_null() && error["error"]["code"] == -32700, "Parse error changed protocol");
    first.ping("after-parse-error");
    // Blank and CR-only lines produce no reply; a BOM and CRLF framing are accepted.
    first.bytes("\n \t\r\n\r\n");
    first.ping("after-blank-lines");
    first.bytes(QByteArray("\xEF\xBB\xBF") +
                QByteArray::fromStdString(Json{{"jsonrpc", "2.0"}, {"id", "bom"}, {"method", "ping"}}.dump()) +
                "\r\n");
    require(first.receive()["id"] == "bom", "BOM or CRLF framing was rejected");
    // A line beyond 1 MiB is discarded through its newline and answered.
    first.bytes(QByteArray(1024 * 1024 + 1, 'x') + "\n");
    auto overlong = first.receive();
    require(overlong["id"].is_null() && overlong["error"]["code"] == -32600, "Overlong line was not rejected");
    first.ping("after-overlong");
    for (const int depth : {65, 10000, 30000}) {
      first.bytes(QByteArray("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\",\"params\":{\"deep\":") +
                  QByteArray(depth, '[') + '0' + QByteArray(depth, ']') + "}}\n");
      auto rejected = first.receive();
      require(rejected["error"]["code"] == -32700, "Deep stdio JSON was not rejected safely");
      first.ping("after-deep-" + std::to_string(depth));
    }
    second.ping("still-alive");
    auto status =
        first.exchange({{"jsonrpc", "2.0"},
                        {"id", "status"},
                        {"method", "tools/call"},
                        {"params", {{"name", "web_search_status"}, {"arguments", Json::object()}}}});
    require(status["result"]["isError"] == false, "Status tool failed");
    first.finish();
    second.finish();
    require(!QFileInfo::exists(temporary.filePath("unused-persistent-cache")),
            "Stdio used shared persistent cache");
    std::cout << "PASS two isolated stdio processes, ID windows, protocol recovery and EOF cleanup\n";
    return 0;
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
