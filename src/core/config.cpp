#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHostAddress>
#include <QLockFile>
#include <QRandomGenerator>
#include <QRegularExpression>
#include <QSaveFile>
#include <QSet>
#include <QStringList>
#include <QTcpServer>
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

void validateCredential(const QByteArray &token, bool required, const char *errorCode, int minimumLength = 32) {
  if ((required && token.size() < minimumLength) || token.size() > 8192)
    throw Error(errorCode, "Credentials have an invalid length.");
  for (unsigned char c : token)
    if (c < 0x21 || c > 0x7e)
      throw Error(errorCode, "Credentials must contain visible ASCII only.");
}

const QRegularExpression &packageDirectoryPattern() {
  static const QRegularExpression pattern(
      "\\Aqcopilots-websearch(?:-poc)?-([0-9]+\\.[0-9]+\\.[0-9]+)(?:-win64|-win-amd64)?\\z",
      QRegularExpression::CaseInsensitiveOption);
  return pattern;
}

QDir deploymentDirectory(const QFileInfo &config) {
  const QFileInfo folder(config.absolutePath());
  const QFileInfo bin(folder.absolutePath());
  if (config.fileName().compare("runtime.json", Qt::CaseInsensitive) == 0 &&
      folder.fileName().compare("data", Qt::CaseInsensitive) == 0 &&
      bin.fileName().compare("bin", Qt::CaseInsensitive) == 0)
    return bin.absoluteDir();
  return config.absoluteDir();
}

QString packageDirectoryRelease(const QString &path) {
  const QFileInfo info(path);
  if (info.fileName().compare("runtime.json", Qt::CaseInsensitive) != 0 &&
      info.fileName().compare("service.json", Qt::CaseInsensitive) != 0)
    return {};
  return packageDirectoryPattern().match(deploymentDirectory(info).dirName()).captured(1);
}

Config validatedConfiguration(Json value, const QString &path) {
  Config c{std::move(value), QFileInfo(path).absolutePath().toStdString(),
           QFileInfo(path).absoluteFilePath().toStdString()};
  const auto deployment = packageDirectoryRelease(path);
  // Check release ownership before interpreting the configuration's fields.
  // The schema reports malformed version types.
  if (c.value.is_object()) {
    const auto version = c.value.find("project_version");
    if ((!deployment.isEmpty() && (deployment != QLatin1String(QCWS_VERSION) || version == c.value.end())) ||
        (version != c.value.end() && version->is_string() && *version != QCWS_VERSION))
      throw Error("invalid_configuration", "Runtime configuration belongs to a different project release. "
                                           "This executable expects " QCWS_VERSION ".");
  }
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
  (void)businessMcpUrl(c.value.at("business"));
  return c;
}

std::set<int> installedListenerPorts(const QString &configPath) {
  QStringList candidates;
  const auto addPeers = [&](const QDir &root) {
    for (const auto &directory : root.entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot | QDir::NoSymLinks)) {
      const auto name = directory.fileName();
      if (directory.isJunction() ||
          (!packageDirectoryPattern().match(name).hasMatch() &&
           name.compare("qcopilots-websearch", Qt::CaseInsensitive) != 0 &&
           name.compare("qcopilots-websearch-poc", Qt::CaseInsensitive) != 0))
        continue;
      const QDir release(directory.absoluteFilePath());
      candidates.append({release.filePath("bin/data/runtime.json"), release.filePath("runtime.json"),
                         release.filePath("service.json")});
    }
  };
  auto root = deploymentDirectory(QFileInfo(configPath));
  if (!packageDirectoryRelease(configPath).isEmpty() && root.cdUp())
    addPeers(root);
#ifdef _WIN32
  candidates.append(windowsServiceConfigPaths());
  const auto programData = qEnvironmentVariable("ProgramData");
  if (QDir::isAbsolutePath(programData))
    addPeers(QDir(programData));
#else
  addPeers(QDir("/opt"));
  addPeers(QDir("/etc"));
#endif

  QSet<QString> visited;
  std::set<int> ports;
  for (const auto &path : candidates) {
    const QFileInfo peer(path);
    if (!peer.isFile() || peer.isSymLink())
      continue;
    const auto canonical = peer.canonicalFilePath();
#ifdef _WIN32
    const auto key = canonical.toCaseFolded();
#else
    const auto &key = canonical;
#endif
    if (key.isEmpty() || visited.contains(key))
      continue;
    visited.insert(key);
    try {
      const auto value = readJson(canonical);
      for (const auto *name : {"business", "control", "admin"}) {
        const auto listener = value.find(name);
        const bool isAdmin = std::string_view(name) == "admin";
        if (listener == value.end() || !listener->is_object()) {
          if (isAdmin)
            ports.insert(AdminConfig{}.port);
          continue;
        }
        const auto port = listener->find("port");
        if (port != listener->end() && port->is_number_integer() && *port >= 1 && *port <= 65535)
          ports.insert(port->get<int>());
        else if (isAdmin)
          ports.insert(AdminConfig{}.port);
      }
    } catch (const Error &) {
      // A live peer is also protected by the socket bind, even if its file is unreadable.
    }
  }
  return ports;
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
  auto configuration = readJson(resource("runtime.json"));
  configuration["project_version"] = QCWS_VERSION;
  return configuration;
}
Json defaultSettings() {
  return readJson(resource("settings.json"));
}
void assignAvailableListenerPorts(Json &configuration, const QString &configPath) {
  // Keep all three sockets bound until selection finishes so fallback ports are distinct.
  const auto installed = installedListenerPorts(configPath);
  QTcpServer business, control, admin;
  const auto reserve = [&](QTcpServer &listener, int defaultPort) {
    if (!installed.contains(defaultPort) &&
        listener.listen(QHostAddress::LocalHost, static_cast<quint16>(defaultPort)))
      return listener.serverPort();
    for (int attempt = 0; attempt < 64 && listener.listen(QHostAddress::LocalHost, 0); ++attempt) {
      if (!installed.contains(listener.serverPort()))
        return listener.serverPort();
      listener.close();
    }
    throw Error("invalid_configuration", "Cannot reserve a loopback port for the new configuration.");
  };
  const auto setListener = [&](const char *name, QTcpServer &listener) {
    auto &options = configuration[name];
    const auto previousPort = options["port"].get<int>();
    const auto port = reserve(listener, previousPort);
    options["port"] = port;
    const auto previousHost = "127.0.0.1:" + std::to_string(previousPort);
    const auto localHost = "127.0.0.1:" + std::to_string(port);
    auto hosts = Json::array();
    for (const auto &host : options["allowed_hosts"]) {
      const auto updated = host == previousHost ? Json(localHost) : host;
      if (std::find(hosts.begin(), hosts.end(), updated) == hosts.end())
        hosts.push_back(updated);
    }
    if (std::find(hosts.begin(), hosts.end(), localHost) == hosts.end())
      hosts.insert(hosts.begin(), localHost);
    options["allowed_hosts"] = std::move(hosts);
  };
  setListener("business", business);
  setListener("control", control);
  configuration["admin"]["port"] =
      reserve(admin, configuration.value("admin", Json::object()).value("port", AdminConfig{}.port));
}

void initializeConfiguration(const std::string &destination, bool selectAvailablePorts,
                             const QString &stateDirectory) {
  const QFileInfo info(QString::fromStdString(destination));
  const auto deployment = packageDirectoryRelease(info.absoluteFilePath());
  if (!deployment.isEmpty() && deployment != QLatin1String(QCWS_VERSION))
    throw Error("invalid_configuration", "Cannot initialize a directory for another project release.");
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
  if (!QDir().mkpath(info.absolutePath()))
    throw Error("invalid_configuration", "Cannot create configuration directory.");
  QLockFile initializationLock(info.absoluteFilePath() + ".initialize.lock");
  if (!initializationLock.tryLock(5000))
    throw Error("invalid_configuration", "Cannot lock the configuration directory for initialization.");
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
  value["settings"]["directory"] = "state";
  value["cache"]["directory"] = "state";
  if (!stateDirectory.isEmpty()) {
    value["settings"]["directory"] = stateDirectory.toStdString();
    value["cache"]["directory"] = QDir(stateDirectory).filePath("cache").toStdString();
  }
  if (selectAvailablePorts)
    assignAvailableListenerPorts(value, info.absoluteFilePath());
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

std::string businessMcpUrl(const Json &business, int boundPort) {
  if (const auto setting = business.find("public_url"); setting != business.end()) {
    const auto &raw = setting->get_ref<const std::string &>();
    const QUrl url(QString::fromStdString(raw), QUrl::StrictMode);
    const auto separator = raw.find("://");
    const auto path = separator == std::string::npos ? std::string::npos : raw.find('/', separator + 3);
    const auto host = url.host();
    QHostAddress address;
    bool validHost = !host.isEmpty() && !host.contains('%');
    if (validHost && !address.setAddress(host)) {
      auto dns = QString::fromLatin1(QUrl::toAce(host));
      if (dns.endsWith('.'))
        dns.chop(1);
      static const QRegularExpression label("\\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\z");
      validHost = !dns.isEmpty() && dns.size() <= 253;
      for (const auto &part : dns.split('.'))
        validHost = validHost && label.match(part).hasMatch();
    }
    if (!url.isValid() || url.scheme() != "https" || !validHost ||
        raw.find('@') != std::string::npos ||
        url.authority(QUrl::FullyEncoded).contains('@') || url.authority(QUrl::FullyEncoded).endsWith(':') ||
        url.hasQuery() || url.hasFragment() || url.port(443) < 1 || url.port(443) > 65535 ||
        path == std::string::npos || raw.substr(path) != "/mcp" ||
        std::any_of(raw.begin(), raw.end(), [](unsigned char ch) { return ch <= 0x20 || ch == 0x7f; }))
      throw Error("invalid_configuration", "Business public_url must be an HTTPS endpoint ending exactly in /mcp.");
    return url.toEncoded(QUrl::FullyEncoded).toStdString();
  }
  const auto host = business.at("host").get<std::string>();
  const auto port = boundPort >= 0 ? boundPort : business.at("port").get<int>();
  if (port < 1 || port > 65535)
    throw Error("invalid_configuration", "Cannot describe an unbound business endpoint.");
  return std::string(business.contains("tls") ? "https://" : "http://") +
         (host.find(':') == std::string::npos ? host : "[" + host + "]") + ":" + std::to_string(port) + "/mcp";
}

std::string Config::path(const std::string &pathValue) const {
  if (pathValue.empty())
    return {};
  return QFileInfo(QDir(QString::fromStdString(baseDirectory)), QString::fromStdString(pathValue))
      .absoluteFilePath()
      .toStdString();
}
std::string Config::secret(const Json &source, bool required, int minimumLength) const {
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
  validateCredential(token, required, "invalid_configuration", minimumLength);
  return token.toStdString();
}
std::vector<ClientIdentity> Config::clients() const {
  std::vector<ClientIdentity> result;
  std::set<std::string> ids;
  for (const auto &c : value["clients"]) {
    const auto id = c["id"].get<std::string>();
    QByteArray hash;
    if (c.contains("token_sha256")) {
      hash = QByteArray::fromHex(QByteArray::fromStdString(c["token_sha256"].get<std::string>()));
    } else {
      try {
        hash = QCryptographicHash::hash(QByteArray::fromStdString(secret(c, true, 1)),
                                       QCryptographicHash::Sha256);
      } catch (const Error &error) {
        throw error.withReason("credential_source_unavailable", {{"client_id", id}});
      }
    }
    if (hash.size() != 32)
      throw Error("invalid_configuration", "Invalid client credential digest.")
          .withReason("credential_digest_mismatch", {{"client_id", id}});
    if (c.contains("token_value")) {
      const auto token = QByteArray::fromStdString(c.at("token_value").get<std::string>());
      try {
        validateCredential(token, true, "invalid_configuration", 1);
      } catch (const Error &error) {
        throw error.withReason("client_credential_invalid", {{"client_id", id}});
      }
      if (QCryptographicHash::hash(token, QCryptographicHash::Sha256) != hash)
        throw Error("invalid_configuration", "Stored credential and digest do not match.")
            .withReason("credential_digest_mismatch", {{"client_id", id}});
    }
    if (!ids.insert(id).second)
      throw Error("invalid_configuration", "Client ID already exists.")
          .withReason("client_id_exists", {{"client_id", id}});
    const auto duplicate = std::find_if(result.begin(), result.end(),
                                        [&](const auto &identity) { return identity.tokenHash == hash; });
    if (duplicate != result.end())
      throw Error("invalid_configuration", "This credential is already assigned to another client.")
          .withReason("client_credential_duplicate", {{"client_id", duplicate->id}});
    const auto generation = c.value("generation", std::string{});
    result.push_back({id, hash, generation.empty() ? id : id + ":" + generation});
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
  if (credential.empty() || credential.size() > 8192 ||
      std::any_of(credential.begin(), credential.end(), [](unsigned char ch) { return ch < 0x21 || ch > 0x7e; }))
    throw Error("unauthorized", "A valid bearer credential is required.", 401);
  const QByteArrayView bytes(credential.data(), qsizetype(credential.size()));
  const auto supplied = QCryptographicHash::hash(bytes, QCryptographicHash::Sha256);
  std::string identity;
  for (const auto &c : clients) {
    unsigned char diff = 0;
    for (qsizetype i = 0; i < supplied.size(); ++i)
      diff |= static_cast<unsigned char>(supplied[i] ^ c.tokenHash[i]);
    if (diff == 0)
      identity = c.principal.empty() ? c.id : c.principal;
  }
  if (identity.empty())
    throw Error("unauthorized", "A valid bearer credential is required.", 401);
  return identity;
}
} // namespace qws
