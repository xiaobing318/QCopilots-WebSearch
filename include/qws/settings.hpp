#pragma once
#include <QByteArray>
#include <mutex>
#include <qws/config.hpp>

namespace qws {
struct SettingsSnapshot {
  Json value;
  Json keys;
  std::string revision;
};

struct ProviderConfig {
  std::string id, url, caFile, key;
  std::vector<std::string> engines;
  bool ready() const;
  std::string fingerprint() const;
};

class SettingsStore {
public:
  explicit SettingsStore(Config config);
  std::shared_ptr<const SettingsSnapshot> current() const;
  ProviderConfig provider(const SettingsSnapshot &, std::string id = {}) const;
  Json view() const;
  Json policy(const SettingsSnapshot &) const;
  std::string error() const;
  Json errorDetails() const;
  Json update(const Json &patch, const std::string &expectedRevision);
  Json setKey(const std::string &id, const std::string &key);
  Json clearKey(const std::string &id);
  void reloadIfChanged();

private:
  Config config_;
  std::string directory_;
  Json defaults_;
  std::string legacyToken_;
  mutable std::mutex mutex_;
  std::shared_ptr<const SettingsSnapshot> active_;
  QByteArray settingsStamp_, secretsStamp_;
  std::string error_;
  Json errorDetails_ = nullptr;
  Json view(const SettingsSnapshot &) const;
};

struct AdminConfig {
  bool enabled = false;
  std::string host, origin, token;
  int port = 8091;
  int sessionIdleMinutes = 30, sessionMaxHours = 12;
  Json hosts, origins;
};

AdminConfig adminConfiguration(const Config &config, bool readToken = true);
QByteArray generateToken();
void checkSettings(const Config &config);
} // namespace qws
