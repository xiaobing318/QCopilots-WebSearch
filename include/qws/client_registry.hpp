#pragma once
#include <atomic>
#include <memory>
#include <mutex>
#include <qws/config.hpp>

namespace qws {
std::string clientRegistryPath(const Config &config);

// HTTP owns the active identity snapshot. Offline CLI writes use the same file
// transaction but intentionally leave an already-running server unchanged.
class ClientRegistry {
public:
  explicit ClientRegistry(Config config);
  Json view() const;
  Json reveal(const std::string &id, const std::string &revision) const;
  Json create(const std::string &id, const QByteArray &token, const std::string &revision);
  Json replace(const std::string &id, const QByteArray &token, const std::string &revision);
  Json remove(const std::string &id, const std::string &revision);
  std::string authorize(const std::string &authorization) const;

private:
  struct Snapshot {
    Json entries;
    std::vector<ClientIdentity> identities;
  };
  Config config_;
  mutable std::mutex managementMutex_;
  std::atomic<std::shared_ptr<const Snapshot>> active_;
  Json change(const std::string &id, const QByteArray *token, const std::string &revision,
              bool createOnly);
};
} // namespace qws
