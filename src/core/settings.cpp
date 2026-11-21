#include <QCryptographicHash>
#include <QDir>
#include <QFileInfo>
#include <QRandomGenerator>
#include <QSaveFile>
#include <QUrl>
#include <algorithm>
#include <array>
#include <qws/settings.hpp>
#include <qws/windows.hpp>

namespace qws {
namespace {
const Json emptySecrets = {{"schema_version", 1}, {"keys", Json::object()}};
const auto ownerReadWrite = QFileDevice::ReadOwner | QFileDevice::WriteOwner;
constexpr std::array keyedProviders{"searxng", "brave", "tavily", "exa", "perplexity", "deepseek"};

QByteArray hash(const QByteArray &data) {
  return QCryptographicHash::hash(data, QCryptographicHash::Sha256).toHex();
}

Json decode(const QByteArray &bytes, const char *schemaName, bool secret = false) {
  auto result = parseBoundedJson({bytes.constData(), size_t(bytes.size())});
  if (result.is_discarded())
    throw Error("invalid_configuration", "Settings files must contain valid JSON.")
        .withReason(secret ? "secrets_json_invalid" : "settings_json_invalid");
  try {
    validateSchema(readSchema(schemaName), result);
  } catch (const Error &error) {
    if (error.code != "invalid_argument")
      throw;
    // Schema diagnostics may contain provider keys. Only a fixed safe message
    // and the affected storage category may leave this layer.
    throw error.withMessage("Saved configuration does not match its schema.")
        .withReason(secret ? "secrets_schema_invalid" : "settings_schema_invalid");
  }
  return result;
}

QByteArray loadFile(const QString &path, bool secret = false) {
  const QFileInfo info(path);
  if (info.isSymLink() || info.isJunction())
    throw Error("invalid_configuration", "Settings files cannot be linked files.")
        .withReason(secret ? "secrets_path_invalid" : "settings_path_invalid");
  if (!info.exists())
    return {};
  if (!info.isFile())
    throw Error("invalid_configuration", "Settings files must be regular files.")
        .withReason(secret ? "secrets_path_invalid" : "settings_path_invalid");
  if (info.size() <= 0 || info.size() > 128 * 1024)
    throw Error("invalid_configuration", "Settings files must be nonempty and bounded.")
        .withReason(secret ? "secrets_file_size_invalid" : "settings_file_size_invalid");
#ifdef _WIN32
  if (secret) {
    try {
      return readWindowsServicePrivateFile(path);
    } catch (const Error &error) {
      throw error.withMessage("Cannot read protected provider key storage.")
          .withReason("secrets_read_failed");
    }
  }
#else
  if (secret && (info.permissions() & (QFileDevice::ReadGroup | QFileDevice::WriteGroup |
                                       QFileDevice::ExeGroup | QFileDevice::ReadOther |
                                       QFileDevice::WriteOther | QFileDevice::ExeOther)))
    throw Error("invalid_configuration", "Secrets must be accessible only to the owner.")
        .withReason("secrets_permissions_invalid");
#endif
  QFile file(path);
  if (!file.open(QIODevice::ReadOnly))
    throw Error("invalid_configuration", "Cannot read settings file.")
        .withReason(secret ? "secrets_read_failed" : "settings_read_failed");
  auto bytes = file.read(128 * 1024 + 1);
  if (bytes.isEmpty() || bytes.size() > 128 * 1024)
    throw Error("invalid_configuration", "Cannot read bounded settings file.")
        .withReason(secret ? "secrets_read_failed" : "settings_read_failed");
  return bytes;
}

void saveFile(const QString &path, const QByteArray &data, bool secret = false) {
  const QFileInfo info(path);
  if (info.isSymLink())
    throw Error("internal_error", "Settings destination must be a regular file.", 500)
        .withReason(secret ? "secrets_path_invalid" : "settings_path_invalid");
  if (info.isJunction() || (info.exists() && !info.isFile())) {
#ifdef _WIN32
    if (secret)
      throw Error("invalid_configuration", "Provider key storage must be a regular file.")
          .withReason("secrets_path_invalid");
#endif
    throw Error("internal_error", "Settings destination must be a regular file.", 500)
        .withReason(secret ? "secrets_path_invalid" : "settings_path_invalid");
  }
  if (!QDir().mkpath(info.absolutePath()))
    throw Error("internal_error", "Cannot create the settings directory.", 500)
        .withReason(secret ? "secrets_directory_unavailable" : "settings_directory_unavailable");
#ifdef _WIN32
  if (secret) {
    try {
      writeWindowsServicePrivateFile(path, data);
    } catch (const Error &error) {
      throw error.withMessage("Cannot save protected provider key storage.")
          .withReason("secrets_write_failed");
    }
    return;
  }
#endif
  QSaveFile file(path);
  if (!file.open(QIODevice::WriteOnly) || (secret && !file.setPermissions(ownerReadWrite)) ||
      file.write(data) != data.size() || !file.commit())
    throw Error("internal_error", "Cannot save settings.", 500)
        .withReason(secret ? "secrets_write_failed" : "settings_write_failed");
}

void validateEndpoint(const std::string &endpoint) {
  const QUrl url(QString::fromStdString(endpoint), QUrl::StrictMode);
  if (!url.isValid() || url.host().isEmpty() || !url.userInfo().isEmpty() ||
      url.hasQuery() || url.hasFragment() || (url.scheme() != "http" && url.scheme() != "https"))
    throw Error("invalid_argument", "Use a fixed HTTP(S) search endpoint without credentials or query.")
        .withReason("settings_endpoint_invalid");
}

std::shared_ptr<const SettingsSnapshot> snapshot(Json value, Json keys) {
  validateEndpoint(value["search"]["searxng"]["url"]);
  const auto revision = hash(QByteArray::fromStdString(value.dump())).left(16).toStdString();
  return std::make_shared<const SettingsSnapshot>(SettingsSnapshot{std::move(value), std::move(keys), revision});
}

QString filePath(const std::string &directory, const char *name) {
  return QDir(QString::fromStdString(directory)).filePath(QString::fromLatin1(name));
}

bool providerHasKey(const std::string &id) {
  return std::find(keyedProviders.begin(), keyedProviders.end(), id) != keyedProviders.end();
}
} // namespace

bool ProviderConfig::ready() const {
  return id == "auto" || id == "searxng" || !key.empty();
}

std::string ProviderConfig::fingerprint() const {
  return hash(QByteArray::fromStdString(Json::array({id, url, caFile, engines, key}).dump())).toStdString();
}

SettingsStore::SettingsStore(Config config) : config_(std::move(config)) {
  std::string defaultDirectory = "state";
#ifndef _WIN32
  // systemd supplies this also for installations retaining their old conffile.
  const auto stateDirectory = qEnvironmentVariable("STATE_DIRECTORY");
  if (QDir::isAbsolutePath(stateDirectory) && !stateDirectory.contains(':'))
    defaultDirectory = stateDirectory.toStdString();
#endif
  directory_ = config_.path(config_.value.value("settings", Json::object()).value("directory", defaultDirectory));
  defaults_ = defaultSettings();
  if (const auto legacy = config_.value.find("provider"); legacy != config_.value.end()) {
    defaults_["search"]["provider"] = "searxng";
    defaults_["search"]["searxng"]["url"] = legacy->at("url");
    defaults_["search"]["searxng"]["engines"] =
        legacy->value("engines", defaults_["search"]["searxng"]["engines"]);
    legacyToken_ = config_.secret(*legacy, false);
  }
  active_ = snapshot(defaults_, emptySecrets.at("keys"));
  reloadIfChanged();
}

std::shared_ptr<const SettingsSnapshot> SettingsStore::current() const {
  std::lock_guard lock(mutex_);
  return active_;
}

ProviderConfig SettingsStore::provider(const SettingsSnapshot &settings, std::string id) const {
  const auto &search = settings.value.at("search");
  if (id.empty())
    id = search.at("provider");
  ProviderConfig result;
  result.id = std::move(id);
  result.key = settings.keys.value(result.id, "");
  if (result.id == "searxng") {
    result.url = search["searxng"]["url"];
    result.engines = search["searxng"]["engines"].get<std::vector<std::string>>();
    if (const auto legacy = config_.value.find("provider");
        legacy != config_.value.end() && legacy->at("url") == result.url) {
      result.caFile = config_.path(legacy->value("ca_file", ""));
      if (result.key.empty())
        result.key = legacyToken_;
    }
  }
  return result;
}

Json SettingsStore::view(const SettingsSnapshot &settings) const {
  auto result = settings.value;
  result["revision"] = settings.revision;
  for (const auto *id : keyedProviders)
    result["providers"][id] = {{"key_set", !provider(settings, id).key.empty()}};
  return result;
}
Json SettingsStore::view() const {
  return view(*current());
}
Json SettingsStore::policy(const SettingsSnapshot &settings) const {
  const auto source = provider(settings);
  return {{"search_enabled", settings.value["tools"]["search"]["enabled"]},
          {"open_enabled", settings.value["tools"]["open"]["enabled"]},
          {"search_without_asking", settings.value["search_without_asking"]},
          {"provider", source.id}, {"provider_ready", source.ready()}, {"revision", settings.revision}};
}
std::string SettingsStore::error() const {
  std::lock_guard lock(mutex_);
  return error_;
}
Json SettingsStore::errorDetails() const {
  std::lock_guard lock(mutex_);
  return errorDetails_;
}

Json SettingsStore::update(const Json &patch, const std::string &expectedRevision) {
  try {
    validateSchema(readSchema("settings-update"), {{"patch", patch}, {"revision", expectedRevision}});
  } catch (const Error &error) {
    if (error.code != "invalid_argument")
      throw;
    throw error.withMessage("Invalid settings change.").withReason("settings_patch_invalid");
  }
  std::lock_guard lock(mutex_);
  if (expectedRevision != active_->revision)
    throw Error("revision_conflict", "Settings changed. Reload before saving.", 409)
        .withReason("settings_revision_conflict");
  auto merged = active_->value;
  merged.merge_patch(patch);
  try {
    validateSchema(readSchema("settings"), merged);
  } catch (const Error &error) {
    if (error.code != "invalid_argument")
      throw;
    throw error.withMessage("Settings do not match the required schema.").withReason("settings_schema_invalid");
  }
  const auto replacement = snapshot(std::move(merged), active_->keys);
  const auto data = QByteArray::fromStdString(replacement->value.dump(2) + "\n");
  saveFile(filePath(directory_, "settings.json"), data);
  settingsStamp_ = hash(data);
  active_ = replacement;
  return view(*active_);
}

Json SettingsStore::setKey(const std::string &id, const std::string &key) {
  if (!providerHasKey(id))
    throw Error("invalid_argument", "Unknown provider key.").withReason("provider_unknown");
  if (key.empty() || key.size() > 8192 ||
      std::any_of(key.begin(), key.end(), [](unsigned char ch) { return ch < 0x21 || ch > 0x7e; }))
    throw Error("invalid_argument", "A valid provider key is required.").withReason("provider_key_invalid");
  std::lock_guard lock(mutex_);
  auto settings = *active_;
  settings.keys[id] = key;
  const auto data = QByteArray::fromStdString(Json{{"schema_version", 1}, {"keys", settings.keys}}.dump(2) + "\n");
  saveFile(filePath(directory_, "secrets.json"), data, true);
  secretsStamp_ = hash(data);
  active_ = std::make_shared<const SettingsSnapshot>(std::move(settings));
  return view(*active_);
}

Json SettingsStore::clearKey(const std::string &id) {
  if (!providerHasKey(id))
    throw Error("invalid_argument", "Unknown provider key.").withReason("provider_unknown");
  std::lock_guard lock(mutex_);
  auto settings = *active_;
  settings.keys.erase(id);
  const auto data = QByteArray::fromStdString(Json{{"schema_version", 1}, {"keys", settings.keys}}.dump(2) + "\n");
  saveFile(filePath(directory_, "secrets.json"), data, true);
  secretsStamp_ = hash(data);
  active_ = std::make_shared<const SettingsSnapshot>(std::move(settings));
  return view(*active_);
}

void SettingsStore::reloadIfChanged() {
  std::lock_guard lock(mutex_);
  try {
    const auto settings = loadFile(filePath(directory_, "settings.json"));
    const auto secrets = loadFile(filePath(directory_, "secrets.json"), true);
    const auto settingsHash = hash(settings);
    const auto secretsHash = hash(secrets);
    if (settingsHash != settingsStamp_ || secretsHash != secretsStamp_) {
      active_ = snapshot(settings.isEmpty() ? defaults_ : decode(settings, "settings"),
                         secrets.isEmpty() ? emptySecrets.at("keys") : decode(secrets, "secrets", true).at("keys"));
      settingsStamp_ = settingsHash;
      secretsStamp_ = secretsHash;
    }
    error_.clear();
    errorDetails_ = nullptr;
  } catch (const Error &error) {
    error_ = "settings_error";
    errorDetails_ = errorPayload(error)["error"];
  }
}

QByteArray generateToken() {
  QByteArray bytes(32, Qt::Uninitialized);
  for (auto &byte : bytes)
    byte = char(QRandomGenerator::system()->generate() & 255);
  return bytes.toHex();
}

AdminConfig adminConfiguration(const Config &config, bool readToken) {
  const auto options = config.value.value("admin", Json::object());
  AdminConfig admin;
  admin.host = options.value("host", "127.0.0.1");
  admin.port = options.value("port", 8091);
  admin.sessionIdleMinutes = options.value("session_idle_minutes", 30);
  admin.sessionMaxHours = options.value("session_max_hours", 12);
  const auto authority = (admin.host == "::1" ? "[::1]" : admin.host) + ":" + std::to_string(admin.port);
  admin.origin = options.value("public_origin", "http://" + authority);
  const QUrl origin(QString::fromStdString(admin.origin), QUrl::StrictMode);
  if (!origin.isValid() || origin.host().isEmpty() || !origin.path().isEmpty() ||
      origin.hasQuery() || origin.hasFragment() || !origin.userInfo().isEmpty() ||
      (origin.scheme() != "https" && !(origin.scheme() == "http" && origin.authority().toStdString() == authority)))
    throw Error("invalid_configuration", "Invalid administration origin.");
  admin.hosts = options.value("allowed_hosts", Json::array({authority, origin.authority().toStdString()}));
  admin.origins = options.value("origins", Json::array({admin.origin}));
  const auto tokenFile = config.path(options.value("token_file", "admin.token"));
  const bool tokenConfigured = options.contains("token_env") || options.contains("token_file");
  admin.enabled = options.value("enabled", tokenConfigured || QFileInfo::exists(QString::fromStdString(tokenFile)));
  if (!admin.enabled || !readToken)
    return admin;
  for (auto name : {"business", "control"})
    if (config.value.contains(name) && config.value.at(name).at("port") == admin.port)
      throw Error("invalid_configuration", "Administration needs a distinct listener port.");
#ifdef _WIN32
  if (!options.contains("token_env")) {
    auto bytes = readWindowsAdminCredential(QString::fromStdString(tokenFile)).trimmed();
    if (bytes.size() < 32 || std::any_of(bytes.begin(), bytes.end(), [](unsigned char ch) { return ch < 0x21 || ch > 0x7e; }))
      throw Error("invalid_configuration", "Invalid administration credential.");
    admin.token = bytes.toStdString();
    return admin;
  }
#endif
  admin.token = config.secret(options.contains("token_env") ? Json{{"token_env", options.at("token_env")}}
                                                             : Json{{"token_file", tokenFile}}, true);
  return admin;
}

void checkSettings(const Config &config) {
  SettingsStore settings(config);
  if (!settings.error().empty()) {
    const auto details = settings.errorDetails();
    throw Error("invalid_configuration", details.value("message", "Invalid settings or secrets."))
        .withReason(details.value("reason", ""));
  }
  (void)adminConfiguration(config);
}
} // namespace qws
