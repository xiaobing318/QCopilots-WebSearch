#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QLockFile>
#include <QRegularExpression>
#include <QSaveFile>
#include <algorithm>
#include <filesystem>
#include <qws/client_registry.hpp>
#include <qws/settings.hpp>
#include <qws/windows.hpp>
#ifndef _WIN32
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace qws {
namespace {
// One hundred 8192-byte credentials can require twice their size in JSON
// escaping. Keep the file bounded while accepting every documented value.
constexpr qsizetype maximumSize = 2 * 1024 * 1024;
struct Document {
  Json entries;
  std::string revision;
};
enum class Operation { Create, Replace, Remove, Upsert };

std::string digest(const QByteArray &bytes) {
  return QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex().toStdString();
}

void validateId(const std::string &id) {
  static const QRegularExpression expression("\\A[A-Za-z0-9_.-]{1,64}\\z");
  if (!expression.match(QString::fromStdString(id)).hasMatch())
    throw Error("invalid_argument", "Invalid client ID.").withReason("client_id_invalid");
}

void validateToken(const QByteArray &token) {
  if (token.isEmpty() || token.size() > 8192 ||
      std::any_of(token.begin(), token.end(), [](unsigned char ch) { return ch < 0x21 || ch > 0x7e; }))
    throw Error("invalid_argument", "Use 1 to 8192 visible ASCII characters for a client credential.")
        .withReason("client_credential_invalid");
}

void validateEntries(const Json &entries) {
  try {
    validateSchema(readSchema("client-registry"), {{"schema_version", 1}, {"clients", entries}});
  } catch (const Error &) {
    throw Error("invalid_configuration", "Invalid client registry.").withReason("client_registry_invalid");
  }
}

QByteArray readRegistry(const QString &path) {
  const QFileInfo info(path);
  if (!info.isFile() || info.isSymLink() || info.isJunction() || info.size() <= 0)
    throw Error("invalid_configuration", "Client registry must be a nonempty regular file.")
        .withReason("client_registry_invalid");
  if (info.size() > maximumSize)
    throw Error("invalid_configuration", "Client registry exceeds its size limit.")
        .withReason("client_registry_too_large");
#ifdef _WIN32
  try {
    return readWindowsServicePrivateFile(path, maximumSize);
  } catch (const Error &) {
    throw Error("invalid_configuration", "Cannot read the protected client registry.")
        .withReason("client_registry_unreadable");
  }
#else
  struct stat metadata{};
  const auto native = QFile::encodeName(path);
  if (::lstat(native.constData(), &metadata) != 0 || !S_ISREG(metadata.st_mode) || metadata.st_nlink != 1 ||
      (metadata.st_mode & 0077))
    throw Error("invalid_configuration", "Client registry must be private to its owner.")
        .withReason("client_registry_permissions");
  QFile file(path);
  if (!file.open(QIODevice::ReadOnly))
    throw Error("invalid_configuration", "Cannot read client registry.").withReason("client_registry_unreadable");
  const auto data = file.read(maximumSize + 1);
  if (data.isEmpty() || data.size() > maximumSize)
    throw Error("invalid_configuration", "Cannot read bounded client registry.")
        .withReason("client_registry_unreadable");
  return data;
#endif
}

Document readDocument(const Config &config) {
  const auto path = QString::fromStdString(clientRegistryPath(config));
  const QFileInfo registry(path);
#ifdef _WIN32
  const std::filesystem::path native(path.toStdWString());
#else
  const std::filesystem::path native(QFile::encodeName(path).toStdString());
#endif
  std::error_code error;
  const auto status = std::filesystem::symlink_status(native, error);
  if (error && error != std::errc::no_such_file_or_directory)
    throw Error("invalid_configuration", "Cannot inspect client registry.")
        .withReason("client_registry_unreadable");
  if (registry.isSymLink() || registry.isJunction())
    throw Error("invalid_configuration", "Client registry cannot be a linked file.")
        .withReason("client_registry_invalid");
  if (std::filesystem::exists(status)) {
    const auto bytes = readRegistry(path);
    const auto value = parseBoundedJson({bytes.constData(), size_t(bytes.size())});
    try {
      validateSchema(readSchema("client-registry"), value);
    } catch (const Error &) {
      throw Error("invalid_configuration", "Invalid client registry.").withReason("client_registry_invalid");
    }
    return {value.at("clients"), digest(QByteArray("registry:") + bytes)};
  }
  // The runtime is only a migration seed. Once a registry exists, even an empty
  // one, it is authoritative and stale runtime credentials cannot return.
  const auto runtime = config.sourcePath.empty() ? config : Config::load(config.sourcePath);
  const auto entries = runtime.value.at("clients");
  validateEntries(entries);
  return {entries, digest(QByteArray("runtime:") + QByteArray::fromStdString(entries.dump()))};
}

std::vector<ClientIdentity> identities(const Config &config, const Json &entries) {
  auto effective = config;
  effective.value["clients"] = entries;
  return effective.clients();
}

Json viewOf(const Document &document, const Json &active) {
  auto clients = Json::array();
  for (const auto &entry : document.entries) {
    const auto source = entry.contains("token_sha256") ? "manual" :
                        entry.contains("token_file") ? "file" : "environment";
    clients.push_back({{"id", entry.at("id")}, {"source", source},
                       {"can_reveal", !entry.contains("token_sha256") || entry.contains("token_value")}});
  }
  return {{"revision", document.revision}, {"clients", std::move(clients)},
          {"pending_restart", document.entries != active}};
}

void prepareDirectory(const QString &directory) {
  const QFileInfo before(directory);
  if (before.isSymLink() || before.isJunction())
    throw Error("invalid_configuration", "Client state directory cannot be linked.")
        .withReason("client_registry_directory_unavailable");
  if (!before.exists()) {
    if (!QDir().mkpath(directory))
      throw Error("invalid_configuration", "Cannot create client state directory.")
          .withReason("client_registry_directory_unavailable");
#ifndef _WIN32
    if (!QFile::setPermissions(directory, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner))
      throw Error("invalid_configuration", "Cannot protect client state directory.")
          .withReason("client_registry_permissions");
#endif
  }
  if (!QFileInfo(directory).isDir())
    throw Error("invalid_configuration", "Client state path must be a directory.")
        .withReason("client_registry_directory_unavailable");
}

#ifndef _WIN32
void protectRegistryLock(const QLockFile &lock) {
  // QLockFile reopens a stale lock for writing before it can remove it. A lock
  // left by a crashed sudo CLI must therefore remain writable by the service.
  const QFileInfo info(lock.fileName());
  struct Descriptor {
    int value;
    ~Descriptor() {
      if (value >= 0)
        ::close(value);
    }
  };
  const auto directoryName = QFile::encodeName(info.absolutePath());
  const Descriptor directory{::open(directoryName.constData(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)};
  struct stat owner{};
  if (directory.value < 0 || ::fstat(directory.value, &owner) != 0 ||
      !S_ISDIR(owner.st_mode) || (owner.st_mode & 0022))
    throw Error("invalid_configuration", "Cannot protect client registry lock directory.")
        .withReason("client_registry_permissions");
  const auto filename = QFile::encodeName(info.fileName());
  const Descriptor file{::openat(directory.value, filename.constData(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC)};
  struct stat metadata{};
  qint64 pid = 0;
  QString host, application;
  if (file.value < 0 || ::fstat(file.value, &metadata) != 0 || !S_ISREG(metadata.st_mode) ||
      metadata.st_nlink != 1 || metadata.st_uid != ::geteuid() ||
      !lock.getLockInfo(&pid, &host, &application) || pid != static_cast<qint64>(::getpid()) ||
      ((metadata.st_uid != owner.st_uid || metadata.st_gid != owner.st_gid) &&
       ::fchown(file.value, owner.st_uid, owner.st_gid) != 0) || ::fchmod(file.value, 0600) != 0)
    throw Error("invalid_configuration", "Cannot protect client registry lock ownership.")
        .withReason("client_registry_permissions");
}
#endif

void saveRegistry(const QString &path, const QByteArray &data) {
  const QFileInfo fileInfo(path);
  if (fileInfo.isSymLink() || fileInfo.isJunction() || (fileInfo.exists() && !fileInfo.isFile()))
    throw Error("invalid_configuration", "Client registry must be a regular file.")
        .withReason("client_registry_invalid");
#ifdef _WIN32
  if (fileInfo.exists())
    (void)readRegistry(path);
  try {
    writeWindowsServicePrivateFile(path, data);
  } catch (const Error &) {
    throw Error("invalid_configuration", "Cannot save the protected client registry.")
        .withReason("client_registry_write_failed");
  }
#else
  struct stat owner{};
  const auto ownerPath = QFile::encodeName(fileInfo.exists() ? path : fileInfo.absolutePath());
  if (::lstat(ownerPath.constData(), &owner) != 0 ||
      (fileInfo.exists() ? (!S_ISREG(owner.st_mode) || owner.st_nlink != 1 || (owner.st_mode & 0077))
                         : (!S_ISDIR(owner.st_mode) || (owner.st_mode & 0022))))
    throw Error("invalid_configuration", "Cannot determine protected client registry ownership.")
        .withReason("client_registry_permissions");
  QSaveFile file(path);
  if (!file.open(QIODevice::WriteOnly) || !file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner))
    throw Error("invalid_configuration", "Cannot create private client registry.")
        .withReason("client_registry_write_failed");
  struct stat temporary{};
  const int handle = file.handle();
  if (handle < 0 || ::fstat(handle, &temporary) != 0 ||
      ((temporary.st_uid != owner.st_uid || temporary.st_gid != owner.st_gid) &&
       ::fchown(handle, owner.st_uid, owner.st_gid) != 0) ||
      ::fchmod(handle, 0600) != 0 || file.write(data) != data.size() || !file.commit())
    throw Error("invalid_configuration", "Cannot save client registry with its service ownership.")
        .withReason("client_registry_write_failed");
#endif
}

struct TransactionResult {
  Document document;
  std::vector<ClientIdentity> identities;
  Json view;
};

TransactionResult transact(const Config &config, Operation operation, const std::string &id,
                           const QByteArray *token, const std::string *revision) {
  validateId(id);
  if (token)
    validateToken(*token);
  if (config.sourcePath.empty())
    throw Error("invalid_configuration", "Client changes require a runtime configuration file.")
        .withReason("client_registry_requires_config");
  const auto path = QString::fromStdString(clientRegistryPath(config));
  prepareDirectory(QFileInfo(path).absolutePath());
  QLockFile fileLock(path + ".lock");
  if (!fileLock.tryLock(5000)) {
    if (fileLock.error() == QLockFile::LockFailedError)
      throw Error("invalid_configuration", "Another process is updating client credentials.")
          .withReason("client_registry_locked");
    if (fileLock.error() == QLockFile::PermissionError)
      throw Error("invalid_configuration", "Client state directory is not writable.")
          .withReason("client_registry_permissions");
    throw Error("invalid_configuration", "Cannot create client registry lock.")
        .withReason("client_registry_write_failed");
  }
#ifndef _WIN32
  protectRegistryLock(fileLock);
#endif
  auto document = readDocument(config);
  if (revision && *revision != document.revision)
    throw Error("revision_conflict", "Client credentials changed. Reload before saving.", 409);
  auto &entries = document.entries;
  auto found = std::find_if(entries.begin(), entries.end(), [&](const auto &entry) { return entry.at("id") == id; });
  const bool exists = found != entries.end();
  if (operation == Operation::Create && exists)
    throw Error("invalid_argument", "Client ID already exists.")
        .withReason("client_id_exists", {{"client_id", id}});
  if ((operation == Operation::Replace || operation == Operation::Remove) && !exists)
    throw Error("not_found", "Client ID was not found.", 404)
        .withReason("client_not_found", {{"client_id", id}});
  if (operation != Operation::Remove && !exists && entries.size() >= 100)
    throw Error("invalid_configuration", "The client limit has been reached.").withReason("client_limit_reached");
  if (operation == Operation::Remove) {
    entries.erase(found);
  } else {
    const auto hash = QCryptographicHash::hash(*token, QCryptographicHash::Sha256);
    for (const auto &other : entries) {
      if (other.at("id") == id)
        continue;
      const auto resolved = identities(config, Json::array({other}));
      if (resolved.front().tokenHash == hash)
        throw Error("invalid_configuration", "This credential is already assigned to another client.")
            .withReason("client_credential_duplicate", {{"client_id", other.at("id")}});
    }
    auto entry = Json{{"id", id}, {"token_sha256", digest(*token)}, {"token_value", token->toStdString()}};
    if (exists) {
      if (found->contains("generation"))
        entry["generation"] = found->at("generation");
      *found = std::move(entry);
    } else {
      entry["generation"] = generateToken().left(32).toStdString();
      entries.push_back(std::move(entry));
    }
  }
  validateEntries(entries);
  auto resolved = identities(config, entries);
  const auto bytes = QByteArray::fromStdString(Json{{"schema_version", 1}, {"clients", entries}}.dump(2) + '\n');
  if (bytes.size() > maximumSize)
    throw Error("invalid_configuration", "Client registry exceeds its size limit.")
        .withReason("client_registry_too_large");
  // Allocate the complete result before committing. Publishing an in-memory
  // snapshot after this transaction does not perform credential I/O.
  document.revision = digest(QByteArray("registry:") + bytes);
  auto view = viewOf(document, entries);
  saveRegistry(path, bytes);
  return {std::move(document), std::move(resolved), std::move(view)};
}
} // namespace

std::string clientRegistryPath(const Config &config) {
  std::string defaultDirectory = "state";
#ifndef _WIN32
  const auto state = qEnvironmentVariable("STATE_DIRECTORY");
  if (QDir::isAbsolutePath(state) && !state.contains(':'))
    defaultDirectory = state.toStdString();
#endif
  const auto directory = config.path(config.value.value("settings", Json::object()).value("directory", defaultDirectory));
  return QDir(QString::fromStdString(directory)).filePath("client-registry.json").toStdString();
}

ClientRegistry::ClientRegistry(Config config) : config_(std::move(config)) {
  auto document = readDocument(config_);
  auto resolved = identities(config_, document.entries);
  active_ = std::make_shared<const Snapshot>(Snapshot{std::move(document.entries), std::move(resolved),
                                                    std::move(document.revision)});
}

Json ClientRegistry::view() const {
  std::lock_guard lock(mutex_);
  return viewOf(readDocument(config_), active_->entries);
}

Json ClientRegistry::reveal(const std::string &id, const std::string &revision) const {
  validateId(id);
  std::lock_guard lock(mutex_);
  const auto document = readDocument(config_);
  if (revision != document.revision)
    throw Error("revision_conflict", "Client credentials changed. Reload before viewing.", 409);
  const auto found = std::find_if(document.entries.begin(), document.entries.end(),
                                  [&](const auto &entry) { return entry.at("id") == id; });
  if (found == document.entries.end())
    throw Error("not_found", "Client ID was not found.", 404)
        .withReason("client_not_found", {{"client_id", id}});
  QByteArray token;
  if (found->contains("token_value")) {
    token = QByteArray::fromStdString(found->at("token_value").get<std::string>());
    validateToken(token);
    if (digest(token) != found->at("token_sha256").get<std::string>())
      throw Error("invalid_configuration", "Stored credential and digest do not match.")
          .withReason("credential_digest_mismatch", {{"client_id", id}});
  } else if (found->contains("token_sha256")) {
    throw Error("invalid_configuration", "This record does not contain a recoverable credential.")
        .withReason("credential_not_recoverable", {{"client_id", id}});
  } else {
    try {
      token = QByteArray::fromStdString(config_.secret(*found, true, 1));
    } catch (const Error &) {
      throw Error("invalid_configuration", "Cannot read this client's credential source.")
          .withReason("credential_source_unavailable", {{"client_id", id}});
    }
  }
  const auto hash = QCryptographicHash::hash(token, QCryptographicHash::Sha256);
  const auto generation = found->value("generation", std::string{});
  const auto principal = generation.empty() ? id : id + ":" + generation;
  const bool active = std::any_of(active_->identities.begin(), active_->identities.end(),
                                  [&](const auto &identity) {
                                    return identity.id == id && identity.principal == principal &&
                                           identity.tokenHash == hash;
                                  });
  return {{"id", id}, {"token", token.toStdString()}, {"revision", document.revision}, {"active", active}};
}

Json ClientRegistry::change(const std::string &id, const QByteArray *token, const std::string &revision,
                            bool createOnly) {
  std::lock_guard lock(mutex_);
  const auto operation = !token ? Operation::Remove : createOnly ? Operation::Create : Operation::Replace;
  auto replacement = std::make_shared<Snapshot>();
  auto result = transact(config_, operation, id, token, &revision);
  replacement->entries = std::move(result.document.entries);
  replacement->identities = std::move(result.identities);
  replacement->revision = std::move(result.document.revision);
  active_ = replacement;
  return std::move(result.view);
}

Json ClientRegistry::create(const std::string &id, const QByteArray &token, const std::string &revision) {
  return change(id, &token, revision, true);
}
Json ClientRegistry::replace(const std::string &id, const QByteArray &token, const std::string &revision) {
  return change(id, &token, revision, false);
}
Json ClientRegistry::remove(const std::string &id, const std::string &revision) {
  return change(id, nullptr, revision, false);
}
std::string ClientRegistry::authorize(const std::string &authorization) const {
  std::shared_ptr<const Snapshot> snapshot;
  {
    std::lock_guard lock(mutex_);
    snapshot = active_;
  }
  return authenticate(snapshot->identities, authorization);
}

void setClientToken(const std::string &path, const std::string &id, const QByteArray &token) {
  (void)transact(Config::load(path), Operation::Upsert, id, &token, nullptr);
}
} // namespace qws
