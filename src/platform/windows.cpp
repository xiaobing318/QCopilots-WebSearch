#include <QCryptographicHash>
#include <QDir>
#include <QFileInfo>
#include <QRandomGenerator>
#include <QRegularExpression>
#include <QUuid>
#include <qws/config.hpp>
#include <qws/client_registry.hpp>
#include <qws/settings.hpp>
#include <qws/windows.hpp>
#define NOMINMAX
#include <aclapi.h>
#include <memory>
#include <sddl.h>
#include <set>
#include <utility>
#include <vector>
#include <windows.h>
#include <shellapi.h>
namespace qws {
namespace {
struct Handle {
  HANDLE value = INVALID_HANDLE_VALUE;
  explicit Handle(HANDLE h) : value(h) {}
  Handle(Handle &&h) noexcept : value(std::exchange(h.value, INVALID_HANDLE_VALUE)) {}
  Handle(const Handle &) = delete;
  ~Handle() {
    if (value && value != INVALID_HANDLE_VALUE)
      CloseHandle(value);
  }
};
struct FreeLocal {
  void operator()(void *p) const {
    if (p)
      LocalFree(p);
  }
};
using Local = std::unique_ptr<void, FreeLocal>;
struct ServiceHandle {
  SC_HANDLE value;
  explicit ServiceHandle(SC_HANDLE h) : value(h) {}
  ServiceHandle(const ServiceHandle &) = delete;
  ~ServiceHandle() {
    if (value)
      CloseServiceHandle(value);
  }
};

QString localAbsolutePath(QString path) {
  path = QDir::fromNativeSeparators(path);
  if (path.startsWith("//?/"))
    path.remove(0, 4);
  if (path.size() < 3 || !path[0].isLetter() || path[1] != ':' || path[2] != '/' || path.indexOf(':', 2) >= 0)
    return {};
  return QDir::cleanPath(path);
}

QString registeredConfigPath(SC_HANDLE manager, const QString &name, const QString &expectedExecutable) {
  const ServiceHandle service(OpenServiceW(manager, name.toStdWString().c_str(), SERVICE_QUERY_CONFIG));
  if (!service.value)
    return {};
  // QueryServiceConfig documents an 8 KiB maximum.
  alignas(QUERY_SERVICE_CONFIGW) BYTE buffer[8192];
  auto *configuration = reinterpret_cast<QUERY_SERVICE_CONFIGW *>(buffer);
  DWORD required = 0;
  if (!QueryServiceConfigW(service.value, configuration, static_cast<DWORD>(sizeof(buffer)), &required) ||
      !(configuration->dwServiceType & SERVICE_WIN32_OWN_PROCESS) || !configuration->lpBinaryPathName ||
      !*configuration->lpBinaryPathName)
    return {};

  int count = 0;
  const Local arguments(CommandLineToArgvW(configuration->lpBinaryPathName, &count));
  if (!arguments || count < 2)
    return {};
  const auto argv = static_cast<LPWSTR *>(arguments.get());
  const QFileInfo program(localAbsolutePath(QString::fromWCharArray(argv[0])));
  const auto canonical = localAbsolutePath(program.canonicalFilePath());
  if (canonical.isEmpty() || !program.isFile() ||
      QFileInfo(canonical).fileName().compare("qcopilots-websearch.exe", Qt::CaseInsensitive) != 0)
    return {};
  if (!expectedExecutable.isEmpty()) {
    const auto expected = localAbsolutePath(QFileInfo(expectedExecutable).canonicalFilePath());
    if (canonical.compare(expected, Qt::CaseInsensitive) != 0)
      return {};
  }

  bool serviceFlag = false, named = false;
  QString path;
  for (int i = 1; i < count; ++i) {
    const auto argument = QString::fromWCharArray(argv[i]);
    if (argument == "--WindowsService" && !serviceFlag) {
      serviceFlag = true;
    } else if (argument == "--ServiceName" && !named && i + 1 < count) {
      if (QString::fromWCharArray(argv[++i]).compare(name, Qt::CaseInsensitive) != 0)
        return {};
      named = true;
    } else if (argument == "--ConfigPath" && path.isEmpty() && i + 1 < count) {
      path = localAbsolutePath(QString::fromWCharArray(argv[++i]));
      if (path.isEmpty())
        return {};
    } else {
      return {};
    }
  }
  return serviceFlag ? path : QString{};
}

bool installed = false;
std::string serviceName;
QByteArray serviceSid;
[[noreturn]] void fail() {
  throw Error("invalid_configuration", "A local file with restricted Windows permissions is required.");
}
std::wstring nativePath(const QString &input) {
  if (input.contains(QChar(u'\0')))
    fail();
  auto p = QDir::toNativeSeparators(QDir::cleanPath(QFileInfo(input).absoluteFilePath()));
  if (p.size() < 3 || !p[0].isLetter() || p[1] != ':' || p[2] != '\\' || p.indexOf(':', 2) >= 0)
    fail();
  return L"\\\\?\\" + p.toStdWString();
}
const QByteArray &userSid() {
  static const QByteArray value = [] {
    HANDLE raw = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw))
      fail();
    Handle token(raw);
    DWORD bytes = 0;
    GetTokenInformation(token.value, TokenUser, nullptr, 0, &bytes);
    if (!bytes)
      fail();
    std::vector<unsigned char> buffer(bytes);
    if (!GetTokenInformation(token.value, TokenUser, buffer.data(), bytes, &bytes))
      fail();
    auto sid = reinterpret_cast<TOKEN_USER *>(buffer.data())->User.Sid;
    return QByteArray(static_cast<const char *>(sid), GetLengthSid(sid));
  }();
  return value;
}
bool trusted(PSID sid) {
  return IsWellKnownSid(sid, WinLocalSystemSid) || IsWellKnownSid(sid, WinBuiltinAdministratorsSid) ||
         (!installed && EqualSid(sid, const_cast<char *>(userSid().constData())));
}
enum class FilePolicy { Private, Admin, ServiceState, AdminEndpoint };
bool serviceWritable(FilePolicy policy) {
  return policy == FilePolicy::ServiceState || policy == FilePolicy::AdminEndpoint;
}
bool interactiveReadable(FilePolicy policy) {
  return policy == FilePolicy::Admin || policy == FilePolicy::AdminEndpoint;
}
void validate(HANDLE file, bool directory = false, bool receipt = false, FilePolicy policy = FilePolicy::Private) {
  FILE_ATTRIBUTE_TAG_INFO attributes{};
  DWORD flags = 0;
  if (GetFileType(file) != FILE_TYPE_DISK ||
      !GetFileInformationByHandleEx(file, FileAttributeTagInfo, &attributes, sizeof(attributes)) ||
      (attributes.FileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DEVICE)) ||
      bool(attributes.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) != directory ||
      !GetVolumeInformationByHandleW(file, nullptr, 0, nullptr, nullptr, &flags, nullptr, 0) ||
      !(flags & FILE_PERSISTENT_ACLS))
    fail();
  PSID owner = nullptr;
  PACL dacl = nullptr;
  PSECURITY_DESCRIPTOR raw = nullptr;
  const auto status =
      GetSecurityInfo(file, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, &owner,
                      nullptr, &dacl, nullptr, &raw);
  Local descriptor(raw);
  if (status != ERROR_SUCCESS || !descriptor || !owner || !IsValidSid(owner) || !dacl || !IsValidAcl(dacl))
    fail();
  if (!trusted(owner) && !(serviceWritable(policy) && !serviceSid.isEmpty() && EqualSid(owner, serviceSid.data())))
    fail();
  GENERIC_MAPPING mapping{FILE_GENERIC_READ, FILE_GENERIC_WRITE, FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS};
  for (DWORD i = 0; i < dacl->AceCount; ++i) {
    void *rawAce = nullptr;
    if (!GetAce(dacl, i, &rawAce))
      fail();
    auto *header = static_cast<ACE_HEADER *>(rawAce);
    if (header->AceFlags & INHERIT_ONLY_ACE)
      continue;
    if (header->AceType == ACCESS_DENIED_ACE_TYPE || header->AceType == ACCESS_DENIED_OBJECT_ACE_TYPE ||
        header->AceType == ACCESS_DENIED_CALLBACK_ACE_TYPE ||
        header->AceType == ACCESS_DENIED_CALLBACK_OBJECT_ACE_TYPE)
      continue;
    if (header->AceType != ACCESS_ALLOWED_ACE_TYPE ||
        header->AceSize < offsetof(ACCESS_ALLOWED_ACE, SidStart) + 8)
      fail();
    auto *ace = static_cast<ACCESS_ALLOWED_ACE *>(rawAce);
    PSID sid = &ace->SidStart;
    auto *s = static_cast<SID *>(sid);
    if (s->SubAuthorityCount > SID_MAX_SUB_AUTHORITIES ||
        header->AceSize <
            offsetof(ACCESS_ALLOWED_ACE, SidStart) + GetSidLengthRequired(s->SubAuthorityCount) ||
        !IsValidSid(sid))
      fail();
    ACCESS_MASK mask = ace->Mask;
    MapGenericMask(&mask, &mapping);
    ACCESS_MASK permitted = trusted(sid) ? FILE_ALL_ACCESS : 0;
    if (!receipt && !serviceSid.isEmpty() && EqualSid(sid, serviceSid.data()))
      permitted |= serviceWritable(policy) ? FILE_ALL_ACCESS :
                   FILE_GENERIC_READ | FILE_GENERIC_EXECUTE;
    if (interactiveReadable(policy) && IsWellKnownSid(sid, WinInteractiveSid))
      permitted |= FILE_GENERIC_READ;
    if (mask & ~permitted)
      fail();
  }
}
QByteArray read(HANDLE file, qint64 limit) {
  LARGE_INTEGER size{};
  if (!GetFileSizeEx(file, &size) || size.QuadPart < 0 || size.QuadPart > limit)
    fail();
  LARGE_INTEGER zero{};
  if (!SetFilePointerEx(file, zero, nullptr, FILE_BEGIN))
    fail();
  QByteArray data;
  char buffer[4096];
  for (;;) {
    DWORD count = 0;
    if (!ReadFile(file, buffer, sizeof(buffer), &count, nullptr))
      fail();
    if (!count)
      break;
    if (data.size() + count > limit)
      fail();
    data.append(buffer, count);
  }
  return data;
}
Json stamp(HANDLE file, const QByteArray &bytes) {
  FILE_ID_INFO id{};
  if (!GetFileInformationByHandleEx(file, FileIdInfo, &id, sizeof(id)))
    fail();
  return {
      {"volume", id.VolumeSerialNumber},
      {"file_id", QByteArray(reinterpret_cast<const char *>(id.FileId.Identifier), 16).toHex().toStdString()},
      {"sha256", QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex().toStdString()}};
}
std::wstring sidText(const QByteArray &sid) {
  LPWSTR text = nullptr;
  if (sid.isEmpty() || !ConvertSidToStringSidW(const_cast<char *>(sid.constData()), &text))
    fail();
  Local holder(text);
  return text;
}
Json create(const QString &path, const QByteArray &bytes, bool receipt,
            FilePolicy policy = FilePolicy::Private) {
  std::wstring sddl = L"D:P(A;;FA;;;SY)(A;;FA;;;BA)";
  if (!installed)
    sddl += L"(A;;FA;;;" + sidText(userSid()) + L")";
  if (!receipt && !serviceSid.isEmpty())
    sddl += (serviceWritable(policy) ? L"(A;;FA;;;" : L"(A;;FRFX;;;") +
            sidText(serviceSid) + L")";
  if (installed && interactiveReadable(policy))
    sddl += L"(A;;FR;;;IU)";
  PSECURITY_DESCRIPTOR raw = nullptr;
  if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &raw, nullptr))
    fail();
  Local descriptor(raw);
  SECURITY_ATTRIBUTES sa{sizeof(sa), descriptor.get(), FALSE};
  Handle file(CreateFileW(nativePath(path).c_str(), GENERIC_READ | GENERIC_WRITE | READ_CONTROL | DELETE,
                          FILE_SHARE_READ, &sa, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr));
  if (file.value == INVALID_HANDLE_VALUE)
    fail();
  try {
    validate(file.value, false, receipt, policy);
    DWORD count = 0;
    if (!WriteFile(file.value, bytes.constData(), static_cast<DWORD>(bytes.size()), &count, nullptr) ||
        count != bytes.size() || !FlushFileBuffers(file.value))
      fail();
    return stamp(file.value, bytes);
  } catch (...) {
    FILE_DISPOSITION_INFO disposition{TRUE};
    SetFileInformationByHandle(file.value, FileDispositionInfo, &disposition, sizeof(disposition));
    throw;
  }
}
bool removeMatching(const QString &path, const Json &expected, bool receipt = false,
                    FilePolicy policy = FilePolicy::Private) {
  Handle file(CreateFileW(nativePath(path).c_str(), GENERIC_READ | READ_CONTROL | DELETE, FILE_SHARE_READ,
                          nullptr, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
  if (file.value == INVALID_HANDLE_VALUE) {
    auto e = GetLastError();
    if (e == ERROR_FILE_NOT_FOUND || e == ERROR_PATH_NOT_FOUND)
      return true;
    fail();
  }
  validate(file.value, false, receipt, policy);
  auto actual = stamp(file.value, read(file.value, 1024 * 1024));
  for (auto key : {"volume", "file_id", "sha256"})
    if (actual[key] != expected[key])
      return false;
  FILE_DISPOSITION_INFO disposition{TRUE};
  if (!SetFileInformationByHandle(file.value, FileDispositionInfo, &disposition, sizeof(disposition)))
    fail();
  return true;
}
void publish(const QString &source, const QString &destination, bool replace = false) {
  if (!MoveFileExW(nativePath(source).c_str(), nativePath(destination).c_str(),
                   MOVEFILE_WRITE_THROUGH | (replace ? MOVEFILE_REPLACE_EXISTING : 0)))
    fail();
}
// Keep every directory in the path open without delete sharing. The installer
// owns the final protected directory, while ancestors need only be non-reparse.
std::vector<Handle> pinParents(const QString &path) {
  const auto absolute = QDir::fromNativeSeparators(QFileInfo(path).absolutePath());
  (void)nativePath(absolute);
  QString current = absolute.left(3);
  auto parts = absolute.mid(3).split('/', Qt::SkipEmptyParts);
  std::vector<Handle> handles;
  for (int i = -1; i < parts.size(); ++i) {
    if (i >= 0)
      current = QDir(current).filePath(parts[i]);
    handles.emplace_back(CreateFileW(nativePath(current).c_str(), FILE_READ_ATTRIBUTES | READ_CONTROL,
                                     FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                                     FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    const auto h = handles.back().value;
    FILE_ATTRIBUTE_TAG_INFO attributes{};
    if (h == INVALID_HANDLE_VALUE ||
        !GetFileInformationByHandleEx(h, FileAttributeTagInfo, &attributes, sizeof(attributes)) ||
        (attributes.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) ||
        !(attributes.FileAttributes & FILE_ATTRIBUTE_DIRECTORY))
      fail();
  }
  validate(handles.back().value, true);
  return handles;
}
QString receiptPath(const QString &path, const QString &transaction) {
  static const QRegularExpression uuidPattern(
      "^\\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\\}?$");
  const QUuid uuid(transaction);
  if (!installed || !uuidPattern.match(transaction).hasMatch() || uuid.isNull())
    fail();
  return QFileInfo(path).absoluteDir().filePath(".qcws-install-" + uuid.toString(QUuid::WithoutBraces) +
                                                ".json");
}
void saveReceipt(const QString &path, const Json &value, bool replace) {
  auto temporary = path + "." + QUuid::createUuid().toString(QUuid::WithoutBraces) + ".tmp";
  const auto signature = create(temporary, QByteArray::fromStdString(value.dump()), true);
  try {
    publish(temporary, path, replace);
  } catch (...) {
    removeMatching(temporary, signature, true);
    throw;
  }
}
bool exists(const QString &path) {
  if (GetFileAttributesW(nativePath(path).c_str()) != INVALID_FILE_ATTRIBUTES)
    return true;
  const auto error = GetLastError();
  if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
    return false;
  fail();
}
void checkConfiguration(const QString &path) {
  Handle file(CreateFileW(nativePath(path).c_str(), GENERIC_READ | READ_CONTROL, FILE_SHARE_READ, nullptr,
                          OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
  if (file.value == INVALID_HANDLE_VALUE)
    fail();
  validate(file.value);
  (void)read(file.value, 1024 * 1024);
  const auto c = Config::load(path.toStdString());
  if (!c.value.contains("project_version"))
    throw Error("invalid_configuration", "An installed Windows service requires a versioned runtime configuration.");
  (void)ClientRegistry(c);
  checkSettings(c);
}
} // namespace
QString windowsServiceConfigPath(const QString &name, const QString &executable) {
  const ServiceHandle manager(OpenSCManagerW(nullptr, nullptr, SC_MANAGER_CONNECT));
  return manager.value ? registeredConfigPath(manager.value, name, executable) : QString{};
}

QStringList windowsServiceConfigPaths() {
  const ServiceHandle manager(OpenSCManagerW(nullptr, nullptr, SC_MANAGER_CONNECT | SC_MANAGER_ENUMERATE_SERVICE));
  if (!manager.value)
    return {};
  static const QRegularExpression servicePattern(
      "\\AQCopilotsWebSearch(?:PoC)?(?:-[0-9]+\\.[0-9]+\\.[0-9]+)?\\z", QRegularExpression::CaseInsensitiveOption);
  // Use paging for hosts with more than the documented maximum per page.
  std::vector<BYTE> buffer(256 * 1024);
  QStringList paths;
  DWORD resume = 0;
  do {
    DWORD required = 0, count = 0;
    const auto previous = resume;
    const bool complete = EnumServicesStatusExW(manager.value, SC_ENUM_PROCESS_INFO, SERVICE_WIN32_OWN_PROCESS,
                                                SERVICE_STATE_ALL, buffer.data(), static_cast<DWORD>(buffer.size()),
                                                &required, &count, &resume, nullptr) != FALSE;
    if (!complete && GetLastError() != ERROR_MORE_DATA)
      break;
    const auto *services = reinterpret_cast<const ENUM_SERVICE_STATUS_PROCESSW *>(buffer.data());
    for (DWORD i = 0; i < count; ++i) {
      const auto name = QString::fromWCharArray(services[i].lpServiceName);
      if (!servicePattern.match(name).hasMatch())
        continue;
      const auto path = registeredConfigPath(manager.value, name, {});
      if (!path.isEmpty())
        paths.push_back(path);
    }
    if (complete || resume == previous)
      break;
  } while (resume != 0);
  return paths;
}

void configureWindowsCredentialPolicy(bool service, const std::string &name) {
  installed = service;
  serviceName = name;
  (void)userSid();
  serviceSid.clear();
  if (!service)
    return;
  DWORD sidSize = 0, domainSize = 0;
  SID_NAME_USE use{};
  auto account = QString::fromStdString("NT SERVICE\\" + name).toStdWString();
  LookupAccountNameW(nullptr, account.c_str(), nullptr, &sidSize, nullptr, &domainSize, &use);
  if (!sidSize)
    fail();
  serviceSid.resize(sidSize);
  std::vector<wchar_t> domain(domainSize);
  if (!LookupAccountNameW(nullptr, account.c_str(), serviceSid.data(), &sidSize, domain.data(), &domainSize,
                          &use))
    fail();
}
QByteArray readWindowsCredential(const QString &path) {
  Handle file(CreateFileW(nativePath(path).c_str(), GENERIC_READ | READ_CONTROL, FILE_SHARE_READ, nullptr,
                          OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
  if (file.value == INVALID_HANDLE_VALUE)
    fail();
  validate(file.value);
  auto bytes = read(file.value, 8192);
  validate(file.value);
  return bytes;
}
void writeWindowsPrivateFile(const QString &path, const QByteArray &contents) {
  auto temporary = path + "." + QUuid::createUuid().toString(QUuid::WithoutBraces) + ".new";
  auto signature = create(temporary, contents, false);
  try {
    publish(temporary, path);
  } catch (...) {
    removeMatching(temporary, signature);
    throw;
  }
}

void replaceWindowsRuntimeConfiguration(const QString &path, const QByteArray &contents) {
  Local descriptor;
  PACL dacl = nullptr;
  SECURITY_DESCRIPTOR_CONTROL control{};
  {
    Handle original(CreateFileW(nativePath(path).c_str(), READ_CONTROL,
                                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                                OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    BY_HANDLE_FILE_INFORMATION info{};
    if (original.value == INVALID_HANDLE_VALUE || !GetFileInformationByHandle(original.value, &info) ||
        (info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) ||
        info.nNumberOfLinks != 1)
      fail();
    PSECURITY_DESCRIPTOR raw = nullptr;
    const auto status = GetSecurityInfo(original.value, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                                        nullptr, nullptr, &dacl, nullptr, &raw);
    descriptor.reset(raw);
    DWORD revision = 0;
    if (status != ERROR_SUCCESS || !dacl || !GetSecurityDescriptorControl(raw, &control, &revision))
      fail();
  }

  // Apply the existing access rules before publishing so the service retains read access.
  const auto temporary = path + "." + QUuid::createUuid().toString(QUuid::WithoutBraces) + ".new";
  auto native = nativePath(temporary);
  (void)create(temporary, contents, false);
  try {
    const auto inheritance = control & SE_DACL_PROTECTED ? PROTECTED_DACL_SECURITY_INFORMATION
                                                         : UNPROTECTED_DACL_SECURITY_INFORMATION;
    if (SetNamedSecurityInfoW(native.data(), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION | inheritance,
                              nullptr, nullptr, dacl, nullptr) != ERROR_SUCCESS)
      fail();
    publish(temporary, path, true);
  } catch (...) {
    DeleteFileW(native.c_str());
    throw;
  }
}

QByteArray readProtectedFile(const QString &path, FilePolicy policy, qint64 limit) {
  Handle file(CreateFileW(nativePath(path).c_str(), GENERIC_READ | READ_CONTROL, FILE_SHARE_READ | FILE_SHARE_DELETE,
                          nullptr, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
  if (file.value == INVALID_HANDLE_VALUE)
    fail();
  validate(file.value, false, false, policy);
  return read(file.value, limit);
}
void writeProtectedFile(const QString &path, const QByteArray &contents, FilePolicy policy, bool replace) {
  const auto temporary = path + "." + QUuid::createUuid().toString(QUuid::WithoutBraces) + ".new";
  auto signature = create(temporary, contents, false, policy);
  try {
    publish(temporary, path, replace);
  } catch (...) {
    removeMatching(temporary, signature, false, policy);
    throw;
  }
}
QByteArray readWindowsAdminCredential(const QString &path) {
  return readProtectedFile(path, FilePolicy::Admin, 8192);
}
void writeWindowsAdminCredential(const QString &path, const QByteArray &contents) {
  writeProtectedFile(path, contents, FilePolicy::Admin, false);
}
QByteArray readWindowsServicePrivateFile(const QString &path, qint64 maximumBytes) {
  return readProtectedFile(path, FilePolicy::ServiceState, maximumBytes);
}
void writeWindowsServicePrivateFile(const QString &path, const QByteArray &contents) {
  writeProtectedFile(path, contents, FilePolicy::ServiceState, true);
}
int readWindowsInstalledAdminPort(const QString &stateDirectory) {
  if (!installed)
    fail();
  const auto manager = OpenSCManagerW(nullptr, nullptr, SC_MANAGER_CONNECT);
  if (!manager)
    fail();
  const auto name = QString::fromStdString(serviceName).toStdWString();
  const auto service = OpenServiceW(manager, name.c_str(), SERVICE_QUERY_STATUS);
  CloseServiceHandle(manager);
  SERVICE_STATUS status{};
  if (!service)
    throw Error("service_unavailable", "The installed Windows service is not running.", 503);
  const bool running = QueryServiceStatus(service, &status) && status.dwCurrentState == SERVICE_RUNNING;
  CloseServiceHandle(service);
  if (!running)
    throw Error("service_unavailable", "The installed Windows service is not running.", 503);

  const auto path = QDir(stateDirectory).filePath("admin-port");
  bool valid = false;
  const auto port = readProtectedFile(path, FilePolicy::AdminEndpoint, 8).trimmed().toInt(&valid);
  if (!valid || port < 0 || port > 65535)
    throw Error("invalid_configuration", "The installed administration endpoint is invalid.");
  if (port == 0)
    throw Error("service_unavailable", "The installed administration endpoint is disabled.", 503);
  return port;
}
void publishWindowsInstalledAdminPort(const QString &stateDirectory, int port) {
  if (!installed || port < 0 || port > 65535)
    fail();
  const auto path = QDir(stateDirectory).filePath("admin-port");
  writeProtectedFile(path, QByteArray::number(port) + '\n', FilePolicy::AdminEndpoint, true);
}
void initializeWindowsTransaction(const QString &path, const QString &transaction) {
  const auto receipt = receiptPath(path, transaction);
  auto parents = pinParents(path);
  Json record{{"version", 1},
              {"transaction", QUuid(transaction).toString(QUuid::WithoutBraces).toStdString()},
              {"service", serviceName},
              {"configuration", QFileInfo(path).fileName().toStdString()},
              {"files", Json::array()}};
  // A stale receipt is never overwritten. An administrator can inspect it.
  saveReceipt(receipt, record, false);
  auto add = [&](const QString &destination, const char *kind, const QByteArray &bytes,
                 FilePolicy policy = FilePolicy::Private) {
    const auto staged = receipt + "." + kind + ".new";
    auto signature = create(staged, bytes, false, policy);
    signature["kind"] = kind;
    record["files"].push_back(signature);
    try {
      // Durable intent precedes publication. Rollback checks file ID and hash.
      saveReceipt(receipt, record, true);
    } catch (...) {
      removeMatching(staged, signature, false, policy);
      throw;
    }
    publish(staged, destination);
  };
  const auto adminPath = QFileInfo(path).absoluteDir().filePath("admin.token");
  if (!exists(adminPath))
    add(adminPath, "admin_token", generateToken() + '\n', FilePolicy::Admin);
  if (exists(path)) {
    checkConfiguration(path);
    return;
  }
  const auto tokenPath = QFileInfo(path).absoluteDir().filePath("client.token");
  if (!exists(tokenPath)) {
    QByteArray random(32, Qt::Uninitialized);
    for (auto &byte : random)
      byte = char(QRandomGenerator::system()->generate() & 255);
    add(tokenPath, "token", random.toHex() + '\n');
  } else {
    Config c{Json::object(), QFileInfo(path).absolutePath().toStdString()};
    (void)c.secret({{"token_file", "client.token"}}, true);
  }
  auto value = defaultConfiguration();
  value["cache"]["directory"] = "state";
  value["clients"] = Json::array({Json{{"id", "local"}, {"token_file", "client.token"}}});
  assignAvailableListenerPorts(value, path);
  add(path, "configuration", QByteArray::fromStdString(value.dump(2) + '\n'));
  checkConfiguration(path);
}
void finishWindowsInitialization(const QString &path, const QString &transaction, bool rollback) {
  const auto receipt = receiptPath(path, transaction);
  auto parents = pinParents(path);
  if (!exists(receipt))
    return;
  Json record, receiptSignature;
  {
    Handle file(CreateFileW(nativePath(receipt).c_str(), GENERIC_READ | READ_CONTROL, FILE_SHARE_READ,
                            nullptr, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (file.value == INVALID_HANDLE_VALUE)
      fail();
    validate(file.value, false, true);
    const auto bytes = read(file.value, 16384);
    record = parseBoundedJson(std::string_view(bytes.constData(), size_t(bytes.size())));
    receiptSignature = stamp(file.value, bytes);
  }
  if (!record.is_object() || record.value("version", 0) != 1 ||
      record.value("transaction", "") != QUuid(transaction).toString(QUuid::WithoutBraces).toStdString() ||
      record.value("service", "") != serviceName ||
      record.value("configuration", "") != QFileInfo(path).fileName().toStdString() ||
      !record.contains("files") || !record["files"].is_array() || record["files"].size() > 3)
    fail();
  std::set<std::string> kinds;
  const QRegularExpression hex32("^[0-9a-f]{32}$"), hex64("^[0-9a-f]{64}$");
  for (const auto &entry : record["files"]) {
    if (!entry.is_object() || !entry.contains("kind") || !entry["kind"].is_string() ||
        !entry.contains("volume") || !entry["volume"].is_number_unsigned() || !entry.contains("file_id") ||
        !entry["file_id"].is_string() || !entry.contains("sha256") || !entry["sha256"].is_string())
      fail();
    const auto kind = entry["kind"].get<std::string>();
    if ((kind != "token" && kind != "admin_token" && kind != "configuration") || !kinds.insert(kind).second ||
        !hex32.match(QString::fromStdString(entry["file_id"])).hasMatch() ||
        !hex64.match(QString::fromStdString(entry["sha256"])).hasMatch())
      fail();
  }
  bool matched = true;
  for (const auto &entry : record["files"]) {
    const auto kind = QString::fromStdString(entry["kind"]);
    const auto directory = QFileInfo(path).absoluteDir();
    const auto destination = kind == "token" ? directory.filePath("client.token") :
                             kind == "admin_token" ? directory.filePath("admin.token") : path;
    const auto policy = kind == "admin_token" ? FilePolicy::Admin : FilePolicy::Private;
    if (rollback)
      matched = removeMatching(destination, entry, false, policy) && matched;
    matched = removeMatching(receipt + "." + kind + ".new", entry, false, policy) && matched;
  }
  if (!matched)
    throw Error("initialization_conflict", "Changed installation files were preserved for inspection.");
  if (!removeMatching(receipt, receiptSignature, true))
    fail();
}
} // namespace qws
