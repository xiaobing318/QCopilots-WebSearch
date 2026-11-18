#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QTemporaryDir>
#include <iostream>
#include <memory>
#include <qws/config.hpp>
#include <qws/windows.hpp>
#include <stdexcept>
#include <vector>
#define NOMINMAX
#include <aclapi.h>
#include <sddl.h>
#include <windows.h>
namespace {
void require(bool ok, const char *message) {
  if (!ok)
    throw std::runtime_error(message);
}
void winRequire(bool ok, const char *operation) {
  if (!ok)
    throw std::runtime_error(std::string(operation) + " failed, Win32=" + std::to_string(GetLastError()));
}
struct Handle {
  HANDLE value;
  explicit Handle(HANDLE h) : value(h) {}
  ~Handle() {
    if (value && value != INVALID_HANDLE_VALUE)
      CloseHandle(value);
  }
  Handle(const Handle &) = delete;
  Handle &operator=(const Handle &) = delete;
};
struct LocalDelete {
  void operator()(void *p) const {
    if (p)
      LocalFree(p);
  }
};
using Local = std::unique_ptr<void, LocalDelete>;
std::wstring native(const QString &path) {
  return L"\\\\?\\" + QDir::toNativeSeparators(QFileInfo(path).absoluteFilePath()).toStdWString();
}
std::wstring currentSid() {
  HANDLE raw = nullptr;
  winRequire(OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw), "OpenProcessToken");
  Handle token(raw);
  DWORD size = 0;
  GetTokenInformation(token.value, TokenUser, nullptr, 0, &size);
  require(size != 0, "No TokenUser size");
  std::vector<unsigned char> bytes(size);
  winRequire(GetTokenInformation(token.value, TokenUser, bytes.data(), size, &size), "GetTokenInformation");
  auto sid = reinterpret_cast<TOKEN_USER *>(bytes.data())->User.Sid;
  LPWSTR text = nullptr;
  winRequire(ConvertSidToStringSidW(sid, &text), "ConvertSidToStringSidW");
  Local storage(text);
  return text;
}
bool elevated() {
  HANDLE raw = nullptr;
  winRequire(OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw), "OpenProcessToken");
  Handle token(raw);
  TOKEN_ELEVATION result{};
  DWORD size = 0;
  winRequire(GetTokenInformation(token.value, TokenElevation, &result, sizeof(result), &size),
             "TokenElevation");
  return result.TokenIsElevated != 0;
}
void setDacl(const QString &path, const std::wstring &sddl) {
  PSECURITY_DESCRIPTOR raw = nullptr;
  winRequire(
      ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &raw, nullptr),
      "Parse SDDL");
  Local descriptor(raw);
  PACL acl = nullptr;
  BOOL present = FALSE, defaulted = FALSE;
  winRequire(GetSecurityDescriptorDacl(descriptor.get(), &present, &acl, &defaulted),
             "GetSecurityDescriptorDacl");
  require(present && acl, "Fixture DACL missing");
  auto pathText = native(path);
  const DWORD status = SetNamedSecurityInfoW(pathText.data(), SE_FILE_OBJECT,
                                             DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                                             nullptr, nullptr, acl, nullptr);
  if (status != ERROR_SUCCESS)
    throw std::runtime_error("SetNamedSecurityInfoW failed, Win32=" + std::to_string(status));
}
void writePlain(const QString &path, const QByteArray &data) {
  Handle file(CreateFileW(native(path).c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_NEW,
                          FILE_ATTRIBUTE_NORMAL, nullptr));
  winRequire(file.value != INVALID_HANDLE_VALUE, "Create fixture");
  DWORD written = 0;
  winRequire(WriteFile(file.value, data.constData(), DWORD(data.size()), &written, nullptr), "Write fixture");
  require(written == DWORD(data.size()), "Short fixture write");
}
bool hasInheritedWorldRead(const QString &path) {
  PACL acl = nullptr;
  PSECURITY_DESCRIPTOR raw = nullptr;
  auto pathText = native(path);
  const DWORD status = GetNamedSecurityInfoW(pathText.data(), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                                             nullptr, nullptr, &acl, nullptr, &raw);
  Local descriptor(raw);
  require(status == ERROR_SUCCESS && acl, "Cannot inspect inherited fixture ACL");
  GENERIC_MAPPING mapping{FILE_GENERIC_READ, FILE_GENERIC_WRITE, FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS};
  for (DWORD i = 0; i < acl->AceCount; ++i) {
    void *rawAce = nullptr;
    winRequire(GetAce(acl, i, &rawAce), "GetAce");
    auto *header = static_cast<ACE_HEADER *>(rawAce);
    if (header->AceType != ACCESS_ALLOWED_ACE_TYPE || !(header->AceFlags & INHERITED_ACE))
      continue;
    auto *ace = static_cast<ACCESS_ALLOWED_ACE *>(rawAce);
    ACCESS_MASK mask = ace->Mask;
    MapGenericMask(&mask, &mapping);
    if (IsWellKnownSid(&ace->SidStart, WinWorldSid) && (mask & FILE_READ_DATA))
      return true;
  }
  return false;
}
template <class F> void rejects(F &&body) {
  bool rejected = false;
  try {
    body();
  } catch (const qws::Error &error) {
    require(error.code == "invalid_configuration", "Unexpected rejection error code");
    rejected = true;
  }
  require(rejected, "Unsafe credential file was accepted");
}
} // namespace
int main(int argc, char **argv) {
  QCoreApplication app(argc, argv);
  try {
    const auto args = app.arguments();
    require(args.size() == 1 || (args.size() == 2 && args[1] == "--RequireUnelevated"),
            "Unknown test argument");
    const bool isElevated = elevated();
    std::cout << "Windows token elevated=" << int(isElevated) << '\n';
    if (args.size() == 2)
      require(!isElevated, "This run must use an unelevated token");
    qws::configureWindowsCredentialPolicy(false, "");
    QTemporaryDir temporary;
    require(temporary.isValid(), "Cannot create temporary fixture directory");
    const QDir root(temporary.path());
    const auto sid = currentSid();
    const std::wstring fileAcl = L"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;" + sid + L")";
    const std::wstring directoryAcl = L"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;" + sid + L")";
    setDacl(root.absolutePath(), directoryAcl);
    const QByteArray value = QByteArray(64, 'a') + '\n';
    int passed = 0, skipped = 0;
    auto test = [&](const char *name, auto &&body) {
      body();
      ++passed;
      std::cout << "PASS " << name << '\n';
    };
    const QString token = root.filePath("private.token");
    test("private token creation and read", [&] {
      qws::writeWindowsPrivateFile(token, value);
      require(qws::readWindowsCredential(token) == value, "Private token content changed");
    });
    test("same-name creation preserves original without leaking temporary files", [&] {
      const auto before = root.entryList(QDir::Files | QDir::Hidden | QDir::NoDotAndDotDot, QDir::Name);
      rejects([&] { qws::writeWindowsPrivateFile(token, QByteArray(64, 'b')); });
      require(qws::readWindowsCredential(token) == value, "Existing token was overwritten");
      require(root.entryList(QDir::Files | QDir::Hidden | QDir::NoDotAndDotDot, QDir::Name) == before,
              "Failed publication leaked a temporary file");
    });
    test("explicit current-user SYSTEM Administrators grants accepted", [&] {
      setDacl(token, fileAcl);
      require(qws::readWindowsCredential(token) == value, "Trusted explicit ACL rejected");
    });
    test("safe inherited grants accepted", [&] {
      const QString child = root.filePath("inherited-safe.token");
      writePlain(child, value);
      require(qws::readWindowsCredential(child) == value, "Safe inherited ACL rejected");
    });
    test("default inheritance from Everyone-readable parent rejected", [&] {
      const QString folder = root.filePath("inherited-public");
      require(root.mkdir("inherited-public"), "Cannot create inheritance fixture");
      setDacl(folder, directoryAcl + L"(A;OICI;FR;;;WD)");
      const QString child = QDir(folder).filePath("inherited.token");
      writePlain(child, value);
      require(hasInheritedWorldRead(child), "Fixture did not inherit Everyone read");
      rejects([&] { (void)qws::readWindowsCredential(child); });
    });
    for (const auto &permission : {L"FR", L"FW"}) {
      test("explicit Everyone access rejected", [&] {
        const QString child = root.filePath(QString::fromWCharArray(permission) + ".token");
        qws::writeWindowsPrivateFile(child, value);
        setDacl(child, fileAcl + L"(A;;" + permission + L";;;WD)");
        rejects([&] { (void)qws::readWindowsCredential(child); });
      });
    }
    test("concurrent writer rejected and release restores access", [&] {
      {
        Handle writer(CreateFileW(native(token).c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                  nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr));
        winRequire(writer.value != INVALID_HANDLE_VALUE, "Open concurrent writer");
        rejects([&] { (void)qws::readWindowsCredential(token); });
      }
      require(qws::readWindowsCredential(token) == value, "Access did not recover");
    });
    test("8192-byte boundary and overflow", [&] {
      const QString exact = root.filePath("boundary.token"), large = root.filePath("oversized.token");
      qws::writeWindowsPrivateFile(exact, QByteArray(8192, 'x'));
      qws::writeWindowsPrivateFile(large, QByteArray(8193, 'x'));
      require(qws::readWindowsCredential(exact).size() == 8192, "Exact boundary rejected");
      rejects([&] { (void)qws::readWindowsCredential(large); });
    });
    test("existing alternate data stream rejected", [&] {
      const QString stream = token + ":credential-test";
      writePlain(stream, value);
      rejects([&] { (void)qws::readWindowsCredential(stream); });
    });
    test("directory rejected",
         [&] { rejects([&] { (void)qws::readWindowsCredential(root.absolutePath()); }); });
    const QString link = root.filePath("credential-link");
    BOOL linked = CreateSymbolicLinkW(native(link).c_str(), native(token).c_str(),
                                      SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE);
    DWORD linkError = linked ? ERROR_SUCCESS : GetLastError();
    if (!linked && linkError == ERROR_INVALID_PARAMETER) {
      linked = CreateSymbolicLinkW(native(link).c_str(), native(token).c_str(), 0);
      linkError = linked ? ERROR_SUCCESS : GetLastError();
    }
    if (linked) {
      test("final-component reparse point rejected", [&] {
        const auto attributes = GetFileAttributesW(native(link).c_str());
        require(attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_REPARSE_POINT),
                "Not a reparse fixture");
        rejects([&] { (void)qws::readWindowsCredential(link); });
        winRequire(DeleteFileW(native(link).c_str()), "Delete fixture link");
        require(qws::readWindowsCredential(token) == value, "Link test changed target");
      });
    } else if (linkError == ERROR_PRIVILEGE_NOT_HELD || linkError == ERROR_NOT_SUPPORTED ||
               linkError == ERROR_INVALID_FUNCTION) {
      ++skipped;
      std::cout << "SKIP final-component reparse point: Win32=" << linkError << '\n';
    } else {
      throw std::runtime_error("Cannot create symlink fixture, Win32=" + std::to_string(linkError));
    }
    std::cout << "Windows security tests: passed=" << passed << " skipped=" << skipped << '\n';
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "Windows security test failed: " << error.what() << '\n';
    return 1;
  }
}
