#pragma once
#include <qws/common.hpp>
#include <functional>
#include <optional>
namespace qws {
class Cache {
public:
  using TimeSource = std::function<std::int64_t()>;
  explicit Cache(std::string path, std::int64_t maxBytes = 256LL * 1024 * 1024,
                 int maxEntries = 10000, TimeSource clock = {});
  ~Cache();
  Cache(const Cache &) = delete;
  Cache &operator=(const Cache &) = delete;
  std::optional<Json> get(const std::string &key);
  void put(const std::string &key, const Json &value, std::int64_t ttlSeconds);
  void erase(const std::string &key);
  Json stats();
  void close();
private:
  struct Impl;
  std::unique_ptr<Impl> d_;
};
}
