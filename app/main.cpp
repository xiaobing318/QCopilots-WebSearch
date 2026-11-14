#include <QCoreApplication>
#include <QRandomGenerator>
#include <QRegularExpression>
#include <QTemporaryDir>
#include <csignal>
#include <iostream>
#include <qws/server.hpp>
#include <qws/windows.hpp>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#endif
namespace {
std::atomic_bool stopping = false;
std::string configPath;
std::wstring windowsServiceName = L"QCopilotsWebSearch";
void signalStop(int) {
  stopping.store(true, std::memory_order_relaxed);
}
int serve() {
  auto config = qws::Config::load(configPath);
  qws::Service service(config);
  qws::HttpServer server(service, config);
  server.start();
  std::cerr << "QCopilots-WebSearch ready.\n";
  while (!stopping.load(std::memory_order_relaxed))
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  server.stop();
  return 0;
}
#ifdef _WIN32
SERVICE_STATUS_HANDLE serviceHandle = nullptr;
SERVICE_STATUS serviceStatus{};
void report(DWORD state, DWORD error = NO_ERROR) {
  serviceStatus.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
  serviceStatus.dwCurrentState = state;
  serviceStatus.dwWin32ExitCode = error;
  serviceStatus.dwControlsAccepted =
      state == SERVICE_RUNNING ? SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN : 0;
  serviceStatus.dwWaitHint = (state == SERVICE_START_PENDING || state == SERVICE_STOP_PENDING) ? 10000 : 0;
  serviceStatus.dwCheckPoint = serviceStatus.dwWaitHint ? serviceStatus.dwCheckPoint + 1 : 0;
  SetServiceStatus(serviceHandle, &serviceStatus);
}
DWORD WINAPI serviceControl(DWORD control, DWORD, void *, void *) {
  if (control == SERVICE_CONTROL_STOP || control == SERVICE_CONTROL_SHUTDOWN) {
    report(SERVICE_STOP_PENDING);
    stopping = true;
  }
  return NO_ERROR;
}
void WINAPI serviceMain(DWORD, wchar_t **) {
  serviceHandle = RegisterServiceCtrlHandlerExW(windowsServiceName.c_str(), serviceControl, nullptr);
  if (!serviceHandle)
    return;
  report(SERVICE_START_PENDING);
  try {
    auto config = qws::Config::load(configPath);
    qws::Service service(config);
    qws::HttpServer server(service, config);
    server.start();
    report(SERVICE_RUNNING);
    while (!stopping.load())
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    server.stop();
    report(SERVICE_STOPPED);
  } catch (...) {
    report(SERVICE_STOPPED, ERROR_SERVICE_SPECIFIC_ERROR);
  }
}
#endif
} // namespace
int main(int argc, char **argv) {
  QCoreApplication application(argc, argv);
  QCoreApplication::setApplicationName("qcopilots-websearch");
  QCoreApplication::setApplicationVersion(QCWS_VERSION);
  bool stdio = false, serviceMode = false, check = false, initialize = false, serviceNameSpecified = false;
  bool rollbackInitialization = false, commitInitialization = false;
  QString transaction;
  const auto args = application.arguments();
  try {
    for (int i = 1; i < args.size(); ++i) {
      if (args[i] == "--ConfigPath" && i + 1 < args.size())
        configPath = args[++i].toStdString();
      else if (args[i] == "--Stdio")
        stdio = true;
      else if (args[i] == "--WindowsService")
        serviceMode = true;
      else if (args[i] == "--RollbackInitialization")
        rollbackInitialization = true;
      else if (args[i] == "--CommitInitialization")
        commitInitialization = true;
      else if (args[i] == "--TransactionId" && i + 1 < args.size())
        transaction = args[++i];
      else if (args[i] == "--ServiceName" && i + 1 < args.size()) {
        const auto name = args[++i];
        if (!QRegularExpression("^[A-Za-z][A-Za-z0-9_.-]{0,63}$").match(name).hasMatch())
          throw qws::Error("invalid_argument", "Invalid Windows service name.");
        windowsServiceName = name.toStdWString();
        serviceNameSpecified = true;
      } else if (args[i] == "--CheckConfig")
        check = true;
      else if (args[i] == "--InitializeConfig")
        initialize = true;
      else if (args[i] == "--Version") {
        std::cout << "QCopilots-WebSearch " << QCWS_VERSION << ", Qt " << qVersion() << '\n';
        return 0;
      } else if (args[i] == "--GenerateToken") {
        QByteArray token(32, Qt::Uninitialized);
        auto *rng = QRandomGenerator::system();
        for (int n = 0; n < 32; n += 4) {
          const auto v = rng->generate();
          std::memcpy(token.data() + n, &v, 4);
        }
        std::cout << token.toHex().toStdString() << '\n';
        return 0;
      } else if (args[i] == "--Help") {
        std::cout << "qcopilots-websearch --ConfigPath FILE [--Stdio | --WindowsService | --CheckConfig | "
                     "--InitializeConfig]\n"
                     "qcopilots-websearch --GenerateToken | --Version | --Help\n";
        return 0;
      } else
        throw qws::Error("invalid_argument", "Unknown or incomplete command-line option.");
    }
    if (configPath.empty() ||
        int(stdio) + int(serviceMode) + int(check) + int(initialize) + int(rollbackInitialization) +
                int(commitInitialization) >
            1 ||
        (!transaction.isEmpty() && !initialize && !rollbackInitialization && !commitInitialization) ||
        ((rollbackInitialization || commitInitialization) && transaction.isEmpty()))
      throw qws::Error("invalid_argument", "Provide --ConfigPath and choose only one operation.");
#ifdef _WIN32
    qws::configureWindowsCredentialPolicy(serviceMode || serviceNameSpecified || !transaction.isEmpty(),
                                          QString::fromStdWString(windowsServiceName).toStdString());
    if (rollbackInitialization || commitInitialization) {
      qws::finishWindowsInitialization(QString::fromStdString(configPath), transaction,
                                       rollbackInitialization);
      return 0;
    }
#else
    if (serviceNameSpecified || !transaction.isEmpty() || rollbackInitialization || commitInitialization)
      throw qws::Error("invalid_argument", "Installer transaction options are available only on Windows.");
#endif
    if (initialize) {
#ifdef _WIN32
      if (!transaction.isEmpty())
        qws::initializeWindowsTransaction(QString::fromStdString(configPath), transaction);
      else
#endif
        qws::initializeConfiguration(configPath);
      std::cout << "Configuration initialized. Existing files were preserved.\n";
      return 0;
    }
    if (check) {
      auto c = qws::Config::load(configPath);
      (void)c.clients();
      (void)c.secret(c.value["provider"], false);
      std::cout << "Configuration valid.\n";
      return 0;
    }
    if (stdio) {
      QTemporaryDir temporary;
      if (!temporary.isValid())
        throw qws::Error("cache_unavailable", "Cannot create private stdio state.", 503);
      auto config = qws::Config::load(configPath);
      config.value["cache"]["directory"] = temporary.path().toStdString();
      qws::Service instance(config);
      return qws::runStdio(instance);
    }
    if (serviceMode) {
#ifdef _WIN32
      SERVICE_TABLE_ENTRYW table[] = {{windowsServiceName.data(), serviceMain}, {nullptr, nullptr}};
      if (!StartServiceCtrlDispatcherW(table))
        throw qws::Error("service_error", "Start this mode through Windows Service Control Manager.", 500);
      return 0;
#else
      throw qws::Error("invalid_argument", "Windows service mode is only available on Windows.");
#endif
    }
    static_assert(std::atomic_bool::is_always_lock_free);
    std::signal(SIGINT, signalStop);
    std::signal(SIGTERM, signalStop);
#ifdef _WIN32
    std::signal(SIGBREAK, signalStop);
#endif
    return serve();
  } catch (const qws::Error &e) {
    std::cerr << e.code << ": " << e.what() << '\n';
    return 1;
  } catch (const std::exception &) {
    std::cerr << "Internal startup failure.\n";
    return 1;
  }
}
