#pragma once
#include <condition_variable>
#include <functional>
#include <mutex>
#include <qws/cache.hpp>
#include <qws/config.hpp>
#include <qws/providers.hpp>
#include <thread>
#include <unordered_map>
#include <vector>
namespace qws {
struct ServiceTransport {
  BackendSearchTransport search;
  std::function<HttpResponse(std::string, std::shared_ptr<CancelToken>)> open;
};
class Service {
public:
  explicit Service(Config config, ServiceTransport transport = {}, Cache::TimeSource time = {});
  ~Service();
  Json tools() const;
  Json call(std::string name, const Json &arguments, std::string principal,
            std::shared_ptr<CancelToken> token);
  Json status() const;
  SettingsStore &settings() { return settings_; }
  Json providerStatus() const { return providers_.status(); }
  Json listeningAddresses() const;
  Json testProvider(const std::string &id);
  void shutdown();
  static std::string normalizeDomain(const std::string &domain);

private:
  struct Job;
  Config config_;
  SettingsStore settings_;
  ProviderRegistry providers_;
  HttpClient http_;
  Cache cache_;
  ServiceTransport transport_;
  int capacity_;
  std::int64_t searchTtl_ = 0, pageTtl_ = 0;
  std::chrono::seconds searchTimeout_{0}, openTimeout_{0};
  std::atomic_int calls_{0}, upstream_{0};
  std::atomic_bool closing_{false};
  mutable std::mutex mutex_;
  std::unordered_map<std::string, std::shared_ptr<Job>> jobs_;
  std::vector<std::shared_ptr<Job>> ownedJobs_;
  std::vector<std::weak_ptr<CancelToken>> callsTokens_;
  Json dependency_ = {{"state", "unknown"}, {"last_checked", nullptr}};
  std::string dependencyFingerprint_;
  void reportDependency(const ProviderConfig &, Json);
  Json search(const Json &, const std::string &, const std::shared_ptr<CancelToken> &, ProviderConfig);
  Json fetchSearch(const Json &, const std::shared_ptr<CancelToken> &, const ProviderConfig &, bool report = true);
  void storeSearch(const std::string &, const Json &);
  Json open(const Json &, const std::string &, const std::shared_ptr<CancelToken> &);
  Json find(const Json &, const std::string &);
  Json page(const std::string &, const std::string &);
  std::string key(const std::string &, const std::string &, const std::string &, const std::string &fingerprint = {}) const;
  void reap();
};
} // namespace qws
