#pragma once

#include <condition_variable>
#include <deque>
#include <mutex>
#include <qws/common.hpp>

namespace qws {
class SearchPacer {
public:
  SearchPacer(std::chrono::milliseconds interval, std::chrono::milliseconds jitter);

  class Lease {
  public:
    ~Lease();
    Lease(const Lease &) = delete;
    Lease &operator=(const Lease &) = delete;

  private:
    friend SearchPacer;
    explicit Lease(SearchPacer &pacer) : pacer_(pacer) {}
    SearchPacer &pacer_;
  };

  Lease acquire(const std::shared_ptr<CancelToken> &,
                Clock::time_point deadline = Clock::time_point::max());
  void accepted();
  int refused(int retryAfter);
  Json status() const;

private:
  std::chrono::milliseconds interval_, jitter_;
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  std::deque<Clock::time_point> starts_;
  Clock::time_point nextStart_ = Clock::time_point::min();
  Clock::time_point blockedUntil_ = Clock::time_point::min();
  int strikes_ = 0;
  bool active_ = false;
};
} // namespace qws
