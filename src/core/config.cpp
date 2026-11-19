#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QLockFile>
#include <QRandomGenerator>
#include <QRegularExpression>
#include <QSaveFile>
#include <QUrl>
#include <algorithm>
#include <memory>
#include <mutex>
#include <nlohmann/json-schema.hpp>
#include <optional>
#include <qws/config.hpp>
#include <qws/settings.hpp>
#include <qws/windows.hpp>
#include <set>
#ifndef _WIN32
#include <sys/stat.h>
#include <unistd.h>
#endif

// Explicitly reference the resource initializer from this static library.
static void initSchemas() {
  Q_INIT_RESOURCE(schemas);
}
namespace qws {
namespace {
using Validator = nlohmann::json_schema::json_validator;
QString resource(const std::string &file) {
  static const bool initialized = [] {
    initSchemas();
    return true;
  }();
  (void)initialized;
  return QString::fromStdString(":/schemas/" + file);
}
Json readJson(const QString &path) {
  QFile file(path);
  if (!file.open(QIODevice::ReadOnly) || file.size() > 1024 * 1024)
    throw Error("invalid_configuration", "Cannot read bounded JSON configuration.");
  const auto bytes = file.readAll();
  auto result = parseBoundedJson(std::string_view(bytes.constData(), size_t(bytes.size())));
  if (result.is_discarded())
    throw Error("invalid_configuration", "Configuration is not valid UTF-8 JSON.");
  return result;
}
struct CompiledSchema {
  Json schema;
  Validator validator;
};
std::mutex schemaMutex;
// Entries are never erased, so a schema's address identifies its compiled
// validator. Validation is const and keeps no per-call state.
std::map<std::string, std::unique_ptr<const CompiledSchema>> compiledSchemas;
const Validator *cachedValidator(const Json &schema) {
  std::lock_guard lock(schemaMutex);
  for (const auto &entry : compiledSchemas)
    if (&entry.second->schema == &schema)
      return &entry.second->validator;
  return nullptr;
}
// Error text can echo client-supplied property names, so bound it on a code
// point boundary to keep the reply valid UTF-8.
std::string boundedText(const std::string &text, int chars) {
  auto s = QString::fromUtf8(text.data(), qsizetype(text.size())).left(chars);
  if (!s.isEmpty() && s.back().isHighSurrogate())
    s.chop(1);
  return s.toStdString();
}
class FirstError final : public nlohmann::json_schema::basic_error_handler {
public:
  std::string where, reason;
  void error(const Json::json_pointer &pointer, const Json &instance, const std::string &message) override {
    basic_error_handler::error(pointer, instance, message);
    if (reason.empty()) {
      where = pointer.empty() ? std::string("the top level") : pointer.to_string();
      reason = message;
    }
  }
};
// Returns "at <JSON pointer>: <reason>" for the first failure.
std::optional<std::string> violation(const Json &schema, const Json &value) {
  std::unique_ptr<Validator> owned;
  const Validator *validator = cachedValidator(schema);
  if (!validator) {
    owned = std::make_unique<Validator>();
    try {
      owned->set_root_schema(schema);
    } catch (const std::exception &) {
      throw Error("internal_error", "Schema cannot be compiled.", 500);
    }
    validator = owned.get();
  }
  FirstError errors;
  try {
    validator->validate(value, errors);
  } catch (const std::exception &) {
    return "at the top level: validation failed";
  }
  if (!errors)
    return std::nullopt;
  return "at " + boundedText(errors.where, 120) + ": " + boundedText(errors.reason, 200);
}

void validateCredential(const QByteArray &token, bool required, const char *errorCode) {
  if ((required && token.size() < 32) || token.size() > 8192)
    throw Error(errorCode, "Client credentials must contain 32 to 8192 visible ASCII characters.");
  for (unsigned char c : token)
    if (c < 0x21 || c > 0x7e)
      throw Error(errorCode, "Credentials must contain visible ASCII only.");
}

Config validatedConfiguration(Json value, const QString &path) {
  Config c{std::move(value), QFileInfo(path).absolutePath().toStdString()};
  if (const auto problem = violation(readSchema("runtime"), c.value))
    throw Error("invalid_configuration", "Runtime configuration does not match its schema " + *problem + ".");
  if (const auto p = c.value.find("provider"); p != c.value.end()) {
    const auto &provider = *p;
    const QUrl url(QString::fromStdString(provider["url"].get<std::string>()), QUrl::StrictMode);
    if (!url.isValid() || url.host().isEmpty() || !url.userInfo().isEmpty() || url.hasQuery() ||
        url.hasFragment() || (url.scheme() != "http" && url.scheme() != "https"))
      throw Error("invalid_configuration",
                  "Provider must be a fixed HTTP(S) endpoint without credentials or query.");
  }
  if (c.value["business"]["host"] == c.value["control"]["host"] &&
      c.value["business"]["port"] == c.value["control"]["port"])
    throw Error("invalid_configuration", "Business and control listeners need distinct ports.");
  return c;
}

#ifndef _WIN32
void replaceRuntimeConfiguration(const QString &path, const QByteArray &contents, const struct stat &metadata) {
  QSaveFile file(path);
  if (!file.open(QIODevice::WriteOnly))
    throw Error("invalid_configuration", "Cannot open runtime configuration for atomic update.");
  const int handle = file.handle();
  struct stat temporary{};
  if (handle < 0 || ::fstat(handle, &temporary) != 0 ||
      ((temporary.st_uid != metadata.st_uid || temporary.st_gid != metadata.st_gid) &&
       ::fchown(handle, metadata.st_uid, metadata.st_gid) != 0) ||
      ::fchmod(handle, metadata.st_mode & 0777) != 0 || file.write(contents) != contents.size() ||
      !file.commit())
    throw Error("invalid_configuration", "Cannot update runtime configuration with its original permissions.");
}
#endif
} // namespace
const Json &readSchema(const std::string &name) {
  const auto path = resource(name + ".schema.json");
  std::lock_guard lock(schemaMutex);
  if (auto it = compiledSchemas.find(name); it != compiledSchemas.end())
    return it->second->schema;
  auto entry = std::make_unique<CompiledSchema>();
  entry->schema = readJson(path);
  try {
    entry->validator.set_root_schema(entry->schema);
  } catch (const std::exception &) {
    throw Error("internal_error", "Bundled schema cannot be compiled.", 500);
  }
  return compiledSchemas.emplace(name, std::move(entry)).first->second->schema;
}
const Json &toolCatalog() {
  static const Json catalog = [] {
    auto value = readJson(resource("tools.json"));
    bool valid = value.is_object() && value.contains("instructions") && value["instructions"].is_string() &&
                 value.contains("tools") && value["tools"].is_array();
    if (valid)
      for (const auto &tool : value["tools"])
        valid = valid && tool.is_object() && tool.contains("name") && tool["name"].is_string() &&
                tool.contains("title") && tool["title"].is_string() && tool.contains("description") &&
                tool["description"].is_string() && tool.contains("annotations") &&
                tool["annotations"].is_object();
    if (!valid)
      throw Error("internal_error", "Bundled tool catalog is malformed.", 500);
    return value;
  }();
  return catalog;
}
Json defaultConfiguration() {
  return readJson(resource("runtime.json"));
}
Json defaultSettings() {
  return readJson(resource("settings.json"));
}
void initializeConfiguration(const std::string &destination) {
#ifndef _WIN32
  // New directories and QSaveFile temporaries start owner-only, so another
  // local user cannot open the credential before its permissions narrow.
  struct OwnerOnly {
    mode_t previous = ::umask(077);
    ~OwnerOnly() {
      ::umask(previous);
    }
  } ownerOnly;
#endif
  const QFileInfo info(QString::fromStdString(destination));
  if (!QDir().mkpath(info.absolutePath()))
    throw Error("invalid_configuration", "Cannot create configuration directory.");
  const auto writeAdmin = [&] {
    const auto path = info.absoluteDir().filePath("admin.token");
    if (QFileInfo::exists(path))
      return;
    const auto encoded = generateToken() + '\n';
#ifdef _WIN32
    writeWindowsAdminCredential(path, encoded);
#else
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly) ||
        !file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner) ||
        file.write(encoded) != encoded.size() || !file.commit())
      throw Error("invalid_configuration", "Cannot save administration credential.");
#endif
  };
  if (info.exists()) {
    auto config = Config::load(destination);
    writeAdmin();
    checkSettings(config);
    return;
  }
  writeAdmin();
  auto value = defaultConfiguration();
  value["cache"]["directory"] = "state";
  value["clients"] = Json::array({Json{{"id", "local"}, {"token_file", "client.token"}}});
  const auto secretPath = info.absoluteDir().filePath("client.token");
  if (!QFileInfo::exists(secretPath)) {
    QByteArray bytes(32, Qt::Uninitialized);
    for (int i = 0; i < 32; ++i)
      bytes[i] = char(QRandomGenerator::system()->generate() & 255);
    const auto encoded = bytes.toHex() + "\n";
#ifdef _WIN32
    writeWindowsPrivateFile(secretPath, encoded);
#else
    QSaveFile file(secretPath);
    if (!file.open(QIODevice::WriteOnly))
      throw Error("invalid_configuration", "Cannot create credential file.");
    file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
    if (file.write(encoded) != encoded.size() || !file.commit())
      throw Error("invalid_configuration", "Cannot save credential file.");
#endif
  }
  const auto json = QByteArray::fromStdString(value.dump(2) + "\n");
#ifdef _WIN32
  writeWindowsPrivateFile(info.absoluteFilePath(), json);
#else
  QSaveFile file(info.absoluteFilePath());
  if (!file.open(QIODevice::WriteOnly))
    throw Error("invalid_configuration", "Cannot create configuration file.");
  file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
  if (file.write(json) != json.size() || !file.commit())
    throw Error("invalid_configuration", "Cannot save configuration file.");
#endif
}
void validateSchema(const Json &schema, const Json &value) {
  if (const auto problem = violation(schema, value))
    throw Error("invalid_argument", "Input does not match the schema " + *problem + ".");
}
Config Config::load(const std::string &path) {
  const auto filename = QString::fromStdString(path);
  return validatedConfiguration(readJson(filename), filename);
}

void setClientToken(const std::string &path, const std::string &id, const QByteArray &token) {
  static const QRegularExpression clientId("\\A[A-Za-z0-9_.-]{1,64}\\z");
  if (!clientId.match(QString::fromStdString(id)).hasMatch())
    throw Error("invalid_argument", "Invalid client ID.");
  validateCredential(token, true, "invalid_argument");
  const QFileInfo requested(QString::fromStdString(path));
  const auto parent = QFileInfo(requested.absolutePath()).canonicalFilePath();
  if (parent.isEmpty())
    throw Error("invalid_configuration", "Cannot find runtime configuration directory.");
  const auto filename = QDir(parent).filePath(requested.fileName());
  QLockFile lock(filename + ".clients.lock");
  if (!lock.tryLock(5000))
    throw Error("invalid_configuration", "Cannot lock runtime configuration for credential update.");
  const QFileInfo current(filename);
  if (!current.isFile() || current.isSymLink())
    throw Error("invalid_configuration", "Runtime configuration must be an existing regular file.");
#ifndef _WIN32
  struct stat metadata{};
  const auto native = QFile::encodeName(filename);
  if (::lstat(native.constData(), &metadata) != 0 || !S_ISREG(metadata.st_mode) || metadata.st_nlink != 1)
    throw Error("invalid_configuration", "Runtime configuration must be a regular file without hard links.");
#endif

  auto value = readJson(filename);
  const auto clients = value.find("clients");
  if (clients == value.end() || !clients->is_array())
    throw Error("invalid_configuration", "Runtime configuration must contain a clients array.");
  const auto hash = QCryptographicHash::hash(token, QCryptographicHash::Sha256);
  auto entry = Json{{"id", id}, {"token_sha256", hash.toHex().toStdString()}};
  const auto found = std::find_if(clients->begin(), clients->end(), [&](const auto &client) {
    return client.is_object() && client.contains("id") && client["id"] == id;
  });
  if (found != clients->end())
    *found = std::move(entry);
  else
    clients->push_back(std::move(entry));
  const auto candidate = validatedConfiguration(std::move(value), filename);
  (void)candidate.clients();
  const auto contents = QByteArray::fromStdString(candidate.value.dump(2) + '\n');
  if (contents.size() > 1024 * 1024)
    throw Error("invalid_configuration", "Updated runtime configuration exceeds its size limit.");
#ifdef _WIN32
  replaceWindowsRuntimeConfiguration(filename, contents);
#else
  replaceRuntimeConfiguration(filename, contents, metadata);
#endif
}
std::string Config::path(const std::string &value) const {
  if (value.empty())
    return {};
  return QFileInfo(QDir(QString::fromStdString(baseDirectory)), QString::fromStdString(value))
      .absoluteFilePath()
      .toStdString();
}
std::string Config::secret(const Json &source, bool required) const {
  QByteArray token;
  const bool configured = source.contains("token_env") || source.contains("token_file");
  if (source.contains("token_env"))
    token = qgetenv(source["token_env"].get<std::string>().c_str());
  else if (source.contains("token_file")) {
#ifdef _WIN32
    token = readWindowsCredential(QString::fromStdString(path(source["token_file"])));
#else
    QFile file(QString::fromStdString(path(source["token_file"])));
    const QFileInfo info(file);
    if (info.isSymLink() || !file.open(QIODevice::ReadOnly) || file.size() > 8192)
      throw Error("invalid_configuration", "Credential file is inaccessible or too large.");
    if (file.permissions() & (QFileDevice::ReadGroup | QFileDevice::WriteGroup | QFileDevice::ExeGroup |
                              QFileDevice::ReadOther | QFileDevice::WriteOther | QFileDevice::ExeOther))
      throw Error("invalid_configuration", "Credential file must be accessible only to its owner.");
    token = file.readAll();
#endif
    if (token.endsWith('\n'))
      token.chop(1);
    if (token.endsWith('\r'))
      token.chop(1);
  }
  if (configured && token.isEmpty())
    throw Error("invalid_configuration", "Configured credentials must be present.");
  validateCredential(token, required, "invalid_configuration");
  return token.toStdString();
}
std::vector<ClientIdentity> Config::clients() const {
  std::vector<ClientIdentity> result;
  std::set<std::string> ids;
  std::set<QByteArray> hashes;
  for (const auto &c : value["clients"]) {
    const auto id = c["id"].get<std::string>();
    const auto hash = c.contains("token_sha256")
                          ? QByteArray::fromHex(QByteArray::fromStdString(c["token_sha256"].get<std::string>()))
                          : QCryptographicHash::hash(QByteArray::fromStdString(secret(c, true)),
                                                     QCryptographicHash::Sha256);
    if (hash.size() != 32)
      throw Error("invalid_configuration", "Invalid client credential digest.");
    if (!ids.insert(id).second || !hashes.insert(hash).second)
      throw Error("invalid_configuration", "Client identities and credentials must be unique.");
    result.push_back({id, hash});
  }
  return result;
}
std::string authenticate(const std::vector<ClientIdentity> &clients, const std::string &authorization) {
  // RFC 9110 section 11.1: the scheme is case-insensitive and followed by 1*SP.
  std::string_view credential(authorization);
  if (credential.size() > 8200 || credential.size() < 8 || qstrnicmp(credential.data(), "Bearer ", 7) != 0)
    throw Error("unauthorized", "A valid bearer credential is required.", 401);
  credential.remove_prefix(7);
  while (!credential.empty() && credential.front() == ' ')
    credential.remove_prefix(1);
  const QByteArrayView bytes(credential.data(), qsizetype(credential.size()));
  const auto supplied = QCryptographicHash::hash(bytes, QCryptographicHash::Sha256);
  std::string identity;
  for (const auto &c : clients) {
    unsigned char diff = 0;
    for (qsizetype i = 0; i < supplied.size(); ++i)
      diff |= static_cast<unsigned char>(supplied[i] ^ c.tokenHash[i]);
    if (diff == 0)
      identity = c.id;
  }
  if (identity.empty())
    throw Error("unauthorized", "A valid bearer credential is required.", 401);
  return identity;
}
} // namespace qws
