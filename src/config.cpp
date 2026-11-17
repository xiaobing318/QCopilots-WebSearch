#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRandomGenerator>
#include <QSaveFile>
#include <QUrl>
#include <memory>
#include <mutex>
#include <nlohmann/json-schema.hpp>
#include <optional>
#include <qws/config.hpp>
#include <qws/windows.hpp>
#include <set>
#ifndef _WIN32
#include <sys/stat.h>
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
  if (info.exists()) {
    (void)Config::load(destination);
    return;
  }
  if (!QDir().mkpath(info.absolutePath()))
    throw Error("invalid_configuration", "Cannot create configuration directory.");
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
  Config c{readJson(QString::fromStdString(path)),
           QFileInfo(QString::fromStdString(path)).absolutePath().toStdString()};
  if (const auto problem = violation(readSchema("runtime"), c.value))
    throw Error("invalid_configuration", "Runtime configuration does not match its schema " + *problem + ".");
  const auto &provider = c.value["provider"];
  const QUrl url(QString::fromStdString(provider["url"].get<std::string>()), QUrl::StrictMode);
  if (!url.isValid() || url.host().isEmpty() || !url.userInfo().isEmpty() || url.hasQuery() ||
      url.hasFragment() || (url.scheme() != "http" && url.scheme() != "https"))
    throw Error("invalid_configuration",
                "Provider must be a fixed HTTP(S) endpoint without credentials or query.");
  if (c.value["business"]["host"] == c.value["control"]["host"] &&
      c.value["business"]["port"] == c.value["control"]["port"])
    throw Error("invalid_configuration", "Business and control listeners need distinct ports.");
  return c;
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
  if ((required && token.size() < 32) || token.size() > 8192 || (configured && token.isEmpty()))
    throw Error("invalid_configuration",
                "Client credentials need at least 32 characters and configured credentials must be present.");
  for (unsigned char c : token)
    if (c < 0x21 || c > 0x7e)
      throw Error("invalid_configuration", "Credentials must contain visible ASCII only.");
  return token.toStdString();
}
std::vector<ClientIdentity> Config::clients() const {
  std::vector<ClientIdentity> result;
  std::set<std::string> ids;
  std::set<QByteArray> hashes;
  for (const auto &c : value["clients"]) {
    const auto id = c["id"].get<std::string>();
    const auto hash =
        QCryptographicHash::hash(QByteArray::fromStdString(secret(c, true)), QCryptographicHash::Sha256);
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
