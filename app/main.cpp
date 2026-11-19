#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QRegularExpression>
#include <QTemporaryDir>
#include <csignal>
#include <cstdio>
#include <iostream>
#include <qws/admin.hpp>
#include <qws/server.hpp>
#include <qws/windows.hpp>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#else
#include <termios.h>
#include <unistd.h>
#endif
namespace {
std::atomic_bool stopping = false;
std::string configPath;
QString adjacentConfiguration() {
  const auto executable = QFileInfo(QCoreApplication::applicationFilePath()).canonicalFilePath();
  const auto directory = executable.isEmpty() ? QCoreApplication::applicationDirPath()
                                              : QFileInfo(executable).absolutePath();
  return QDir(directory).filePath("data/runtime.json");
}
#ifdef _WIN32
std::wstring windowsServiceName = QString::fromLatin1(QCWS_WINDOWS_SERVICE_NAME).toStdWString();

bool sameConfiguration(const QString &first, const QString &second) {
  return QFileInfo(first).absoluteFilePath().compare(QFileInfo(second).absoluteFilePath(), Qt::CaseInsensitive) == 0;
}
#endif

class HiddenTokenInput {
public:
  HiddenTokenInput() {
#ifdef _WIN32
    input = GetStdHandle(STD_INPUT_HANDLE);
    if (!GetConsoleMode(input, &saved))
      return;
    if (!SetConsoleCtrlHandler(restoreOnInterrupt, TRUE))
      throw qws::Error("invalid_argument", "Cannot protect console token input.");
    if (!SetConsoleMode(input, saved & ~ENABLE_ECHO_INPUT)) {
      SetConsoleCtrlHandler(restoreOnInterrupt, FALSE);
      throw qws::Error("invalid_argument", "Cannot hide console token input.");
    }
#else
    if (!isatty(STDIN_FILENO))
      return;
    if (tcgetattr(STDIN_FILENO, &saved) != 0)
      throw qws::Error("invalid_argument", "Cannot read console input mode.");
    struct sigaction handler{};
    handler.sa_handler = restoreOnInterrupt;
    sigemptyset(&handler.sa_mask);
    if (sigaction(SIGINT, &handler, &previous) != 0)
      throw qws::Error("invalid_argument", "Cannot protect console token input.");
    auto mode = saved;
    mode.c_lflag &= ~ECHO;
    if (tcsetattr(STDIN_FILENO, TCSANOW, &mode) != 0) {
      sigaction(SIGINT, &previous, nullptr);
      throw qws::Error("invalid_argument", "Cannot hide console token input.");
    }
#endif
    active = true;
  }
  ~HiddenTokenInput() {
    if (!active)
      return;
#ifdef _WIN32
    SetConsoleMode(input, saved);
    SetConsoleCtrlHandler(restoreOnInterrupt, FALSE);
#else
    tcsetattr(STDIN_FILENO, TCSANOW, &saved);
    sigaction(SIGINT, &previous, nullptr);
#endif
    std::cerr << '\n';
  }
  HiddenTokenInput(const HiddenTokenInput &) = delete;
  HiddenTokenInput &operator=(const HiddenTokenInput &) = delete;

private:
  bool active = false;
#ifdef _WIN32
  static inline HANDLE input = INVALID_HANDLE_VALUE;
  static inline DWORD saved = 0;
  static BOOL WINAPI restoreOnInterrupt(DWORD event) {
    if (event == CTRL_C_EVENT || event == CTRL_BREAK_EVENT)
      SetConsoleMode(input, saved);
    return FALSE;
  }
#else
  static inline struct termios saved{};
  struct sigaction previous{};
  static void restoreOnInterrupt(int signal) {
    tcsetattr(STDIN_FILENO, TCSANOW, &saved);
    _exit(128 + signal);
  }
#endif
};

QByteArray readInputLine(qsizetype limit, bool firstLine) {
  // Windows PowerShell can prefix redirected UTF-8 with a byte-order mark.
  constexpr char utf8Bom[] = "\xef\xbb\xbf";
  const auto capacity = limit + (firstLine ? 3 : 0);
  QByteArray line;
  for (auto c = std::cin.get(); c != EOF && c != '\n'; c = std::cin.get()) {
    if (line.size() == capacity + 1)
      throw qws::Error("invalid_argument", "Console input exceeds its length limit.");
    line += static_cast<char>(c);
  }
  if (firstLine && line.startsWith(utf8Bom))
    line.remove(0, 3);
  if (line.endsWith('\r'))
    line.chop(1);
  if (line.size() > limit)
    throw qws::Error("invalid_argument", "Console input exceeds its length limit.");
  return line;
}

void configureClientToken(const std::string &clientId, bool generate) {
  QByteArray token;
  if (generate) {
    token = qws::generateToken();
  } else {
    HiddenTokenInput hidden;
    std::cerr << "Enter client token: " << std::flush;
    token = readInputLine(8192, true);
  }
  std::cerr << "Write the client credential to runtime configuration? [y/N] " << std::flush;
  const auto reply = readInputLine(32, generate).trimmed().toLower();
  if (reply != "y" && reply != "yes") {
    std::cerr << "Credential was not saved.\n";
    return;
  }
  qws::setClientToken(configPath, clientId, token);
  std::cerr << "Credential saved. Restart the service to apply it.\n";
  if (generate)
    std::cout << token.toStdString() << '\n';
}

void signalStop(int) {
  stopping.store(true, std::memory_order_relaxed);
}
void waitForStop(qws::Service &service) {
  auto nextReload = qws::Clock::now() + std::chrono::seconds(2);
  while (!stopping.load(std::memory_order_relaxed)) {
    if (qws::Clock::now() >= nextReload) {
      service.settings().reloadIfChanged();
      nextReload = qws::Clock::now() + std::chrono::seconds(2);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
}
int serve() {
  auto config = qws::Config::load(configPath);
  qws::Service service(config);
  qws::HttpServer server(service, config);
  server.start();
  std::cerr << "QCopilots-WebSearch ready.\n";
  waitForStop(service);
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
    const QFileInfo runtime(QString::fromStdString(configPath));
    if (QString::fromStdWString(windowsServiceName) == QLatin1String(QCWS_WINDOWS_SERVICE_NAME) &&
        sameConfiguration(runtime.absoluteFilePath(), adjacentConfiguration())) {
      const auto admin = qws::adminConfiguration(config, false);
      qws::publishWindowsInstalledAdminPort(runtime.absoluteDir().filePath("state"), admin.enabled ? admin.port : 0);
    }
    report(SERVICE_RUNNING);
    waitForStop(service);
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
  bool settingsUrl = false, openUrl = false;
  bool generateToken = false, setToken = false, clientIdSpecified = false;
  bool selectAvailablePorts = false;
  bool implicitConfiguration = false;
  std::string clientId = "local";
  std::string serviceName;
  QString transaction, stateDirectory;
  const auto args = application.arguments();
  const bool portableStartup = args.size() == 1;
  try {
    for (int i = 1; i < args.size(); ++i) {
      if (args[i] == "--ConfigPath" && i + 1 < args.size()) {
        const auto path = args[++i];
        if (path.isEmpty())
          throw qws::Error("invalid_argument", "--ConfigPath requires a file path.");
        configPath = path.toStdString();
      } else if (args[i] == "--SettingsUrl")
        settingsUrl = true;
      else if (args[i] == "--Open")
        openUrl = true;
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
        serviceName = name.toStdString();
        serviceNameSpecified = true;
      } else if (args[i] == "--CheckConfig")
        check = true;
      else if (args[i] == "--InitializeConfig")
        initialize = true;
      else if (args[i] == "--SelectAvailablePorts")
        selectAvailablePorts = true;
      else if (args[i] == "--StateDirectory" && i + 1 < args.size()) {
        stateDirectory = args[++i];
        if (!QDir::isAbsolutePath(stateDirectory))
          throw qws::Error("invalid_argument", "State directory must be an absolute path.");
      }
      else if (args[i] == "--GenerateToken")
        generateToken = true;
      else if (args[i] == "--SetToken")
        setToken = true;
      else if (args[i] == "--ClientId" && i + 1 < args.size()) {
        clientId = args[++i].toStdString();
        clientIdSpecified = true;
      } else if (args[i] == "--Version") {
        std::cout << "QCopilots-WebSearch " << QCWS_VERSION << ", Qt " << qVersion() << '\n';
        return 0;
      } else if (args[i] == "--Help") {
        std::cout << "qcopilots-websearch\n"
                     "qcopilots-websearch --ConfigPath FILE [--Stdio | --WindowsService]\n"
                     "qcopilots-websearch [--ConfigPath FILE] --CheckConfig\n"
                     "qcopilots-websearch --ConfigPath FILE --InitializeConfig "
                     "[--SelectAvailablePorts] [--StateDirectory DIR]\n"
                     "qcopilots-websearch [--ConfigPath FILE] --SettingsUrl [--Open]\n"
                     "qcopilots-websearch --ConfigPath FILE (--GenerateToken | --SetToken) [--ClientId ID]\n"
                     "qcopilots-websearch --GenerateToken | --Version | --Help\n";
        return 0;
      } else
        throw qws::Error("invalid_argument", "Unknown or incomplete command-line option.");
    }
    const bool standaloneGenerate = generateToken && configPath.empty();
    if ((!portableStartup && !settingsUrl && (!check || serviceNameSpecified) && !standaloneGenerate &&
         configPath.empty()) ||
        int(stdio) + int(serviceMode) + int(check) + int(initialize) + int(rollbackInitialization) +
                int(commitInitialization) + int(settingsUrl) + int(generateToken) + int(setToken) >
            1 || (openUrl && !settingsUrl) ||
        (clientIdSpecified && ((!generateToken && !setToken) || standaloneGenerate)) ||
        (standaloneGenerate && serviceNameSpecified) ||
        ((selectAvailablePorts || !stateDirectory.isEmpty()) && (!initialize || !transaction.isEmpty())) ||
        (!transaction.isEmpty() && !initialize && !rollbackInitialization && !commitInitialization) ||
        ((rollbackInitialization || commitInitialization) && transaction.isEmpty()))
      throw qws::Error("invalid_argument", "Provide --ConfigPath and choose only one operation.");
    if (standaloneGenerate) {
      std::cout << qws::generateToken().toStdString() << '\n';
      return 0;
    }
    implicitConfiguration = configPath.empty();
    if (implicitConfiguration)
      configPath = adjacentConfiguration().toStdString();
    const QFileInfo runtime(QString::fromStdString(configPath));
#ifdef _WIN32
    const auto registered = implicitConfiguration
                                ? qws::windowsServiceConfigPath(QLatin1String(QCWS_WINDOWS_SERVICE_NAME),
                                                                QCoreApplication::applicationFilePath())
                                : QString{};
    const bool installedConfig = !registered.isEmpty();
    if (portableStartup && installedConfig)
      throw qws::Error("invalid_argument", "Start the installed instance through Windows Services.");
    if (installedConfig && !sameConfiguration(registered, runtime.absoluteFilePath()))
      throw qws::Error("invalid_configuration", "The registered Windows service uses a different configuration path.");
    if (serviceNameSpecified) {
      if (settingsUrl && implicitConfiguration && (!installedConfig || serviceName != QCWS_WINDOWS_SERVICE_NAME))
        throw qws::Error("invalid_argument", "Provide --ConfigPath for a different Windows service.");
      windowsServiceName = QString::fromStdString(serviceName).toStdWString();
    }
    qws::configureWindowsCredentialPolicy(serviceMode || serviceNameSpecified || !transaction.isEmpty() ||
                                              installedConfig,
                                          QString::fromStdWString(windowsServiceName).toStdString());
    if (rollbackInitialization || commitInitialization) {
      qws::finishWindowsInitialization(QString::fromStdString(configPath), transaction,
                                       rollbackInitialization);
      return 0;
    }
#else
    if (serviceNameSpecified || !transaction.isEmpty() || rollbackInitialization || commitInitialization)
      throw qws::Error("invalid_argument", "Installer transaction options are available only on Windows.");
#ifdef __linux__
    if (portableStartup && runtime.absoluteFilePath() ==
                               QLatin1String("/opt/qcopilots-websearch-" QCWS_VERSION "/bin/data/runtime.json"))
      throw qws::Error("invalid_argument", "Start the installed instance through systemd.");
#endif
#endif
    if (generateToken || setToken) {
      configureClientToken(clientId, generateToken);
      return 0;
    }
    if (initialize) {
#ifdef _WIN32
      if (!transaction.isEmpty())
        qws::initializeWindowsTransaction(QString::fromStdString(configPath), transaction);
      else
#endif
        qws::initializeConfiguration(configPath, selectAvailablePorts, stateDirectory);
      std::cout << "Configuration initialized. Existing files were preserved.\n";
      return 0;
    }
    if (settingsUrl) {
      qws::Config config;
#ifdef _WIN32
      if (installedConfig) {
        const auto port = qws::readWindowsInstalledAdminPort(runtime.absoluteDir().filePath("state"));
        config = {qws::Json{{"admin", {{"port", port}}}}, runtime.absolutePath().toStdString()};
      } else
#endif
      {
        config = qws::Config::load(configPath);
      }
      const auto url = qws::settingsUrl(config);
      if (openUrl)
        qws::openSettingsUrl(url);
      std::cout << url << '\n';
      return 0;
    }
    if (check) {
      auto c = qws::Config::load(configPath);
      (void)c.clients();
      qws::checkSettings(c);
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
    if (portableStartup) {
      const QFileInfo directory(runtime.absolutePath());
      if (directory.isSymLink() || directory.isJunction() || runtime.isSymLink())
        throw qws::Error("invalid_configuration", "The portable configuration must be next to the executable.");
      if (!runtime.exists())
        qws::initializeConfiguration(configPath, true);
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
    if (implicitConfiguration)
      std::cerr << "Runtime configuration: " << configPath << '\n';
    return 1;
  } catch (const std::exception &) {
    std::cerr << "Internal startup failure.\n";
    return 1;
  }
}
