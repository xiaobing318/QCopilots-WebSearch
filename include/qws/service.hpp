#pragma once
#include <condition_variable>
#include <functional>
#include <mutex>
#include <qws/cache.hpp>
#include <qws/config.hpp>
#include <qws/network.hpp>
#include <thread>
#include <unordered_map>
#include <vector>
namespace qws {
struct ServiceTransport {
  std::function<HttpResponse(std::string, std::string, std::string, std::shared_ptr<CancelToken>)> search;
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
  void shutdown();
  static std::string normalizeDomain(const std::string &domain);

private:
  struct Job;
  Config config_;
  HttpClient http_;
  Cache cache_;
  ServiceTransport transport_;
  std::string providerId_, providerToken_;
  int capacity_;
  // Read once at construction. Worker threads never index the mutable config.
  std::string providerUrl_, enginesParam_;
  std::vector<std::string> engines_;
  std::int64_t searchTtl_ = 0, pageTtl_ = 0;
  std::chrono::seconds searchTimeout_{0}, openTimeout_{0};
  std::atomic_int calls_{0}, upstream_{0};
  std::atomic_bool closing_{false};
  mutable std::mutex mutex_;
  std::unordered_map<std::string, std::shared_ptr<Job>> jobs_;
  std::vector<std::shared_ptr<Job>> ownedJobs_;
  std::vector<std::weak_ptr<CancelToken>> callsTokens_;
  Json dependency_ = {{"state", "unknown"}, {"last_checked", nullptr}};
  Json search(const Json &, const std::string &, const std::shared_ptr<CancelToken> &);
  Json fetchSearch(const Json &, const std::shared_ptr<CancelToken> &);
  void storeSearch(const std::string &, const Json &);
  Json open(const Json &, const std::string &, const std::shared_ptr<CancelToken> &);
  Json find(const Json &, const std::string &);
  Json page(const std::string &, const std::string &);
  std::string key(const std::string &, const std::string &, const std::string &) const;
  void reap();
};
} // namespace qws
