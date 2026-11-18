#include "pacing.hpp"

#include <QRandomGenerator>
#include <algorithm>

namespace qws {
using namespace std::chrono_literals;
namespace {
constexpr std::size_t RequestsPerMinute = 8;

int secondsUntil(Clock::time_point end, Clock::time_point now) {
  if (end <= now)
    return 0;
  return static_cast<int>(std::chrono::ceil<std::chrono::seconds>(end - now).count());
}

[[noreturn]] void limited(int seconds) {
  throw Error("rate_limited", "Search source is cooling down.", 429, true, seconds);
}
} // namespace

SearchPacer::SearchPacer(std::chrono::milliseconds interval, std::chrono::milliseconds jitter)
    : interval_(interval), jitter_(jitter) {}

SearchPacer::Lease::~Lease() {
  {
    std::lock_guard lock(pacer_.mutex_);
    pacer_.active_ = false;
  }
  pacer_.changed_.notify_all();
}

SearchPacer::Lease SearchPacer::acquire(const std::shared_ptr<CancelToken> &token, Clock::time_point deadline) {
  if (!token)
    throw Error("invalid_argument", "A search deadline is required.");
  deadline = std::min(token->deadline, deadline);
  std::unique_lock lock(mutex_);
  for (;;) {
    const auto now = Clock::now();
    if (token->cancelled.load())
      throw Error("cancelled", "Search was cancelled.", 499);
    if (now >= deadline)
      throw Error("timeout", "Search deadline expired.", 504, true);
    while (!starts_.empty() && starts_.front() + 60s <= now)
      starts_.pop_front();
    if (blockedUntil_ > now)
      limited(secondsUntil(blockedUntil_, now));
    if (starts_.size() == RequestsPerMinute)
      limited(secondsUntil(starts_.front() + 60s, now));
    if (!active_ && nextStart_ <= now) {
      const auto randomDelay =
          std::chrono::milliseconds(QRandomGenerator::global()->bounded(static_cast<quint32>(jitter_.count() + 1)));
      starts_.push_back(now);
      nextStart_ = now + interval_ + randomDelay;
      active_ = true;
      return Lease(*this);
    }
    // Cancellation can be signalled without notifying this condition variable.
    changed_.wait_until(lock, std::min(deadline, now + 25ms));
  }
}

void SearchPacer::accepted() {
  std::lock_guard lock(mutex_);
  strikes_ = 0;
  blockedUntil_ = Clock::time_point::min();
}

int SearchPacer::refused(int retryAfter) {
  std::lock_guard lock(mutex_);
  strikes_ = std::min(strikes_ + 1, 5);
  const int seconds = std::min(600, std::max(60 << (strikes_ - 1), retryAfter));
  blockedUntil_ = Clock::now() + std::chrono::seconds(seconds);
  return seconds;
}

Json SearchPacer::status() const {
  std::lock_guard lock(mutex_);
  const auto now = Clock::now();
  const auto first = std::find_if(starts_.begin(), starts_.end(), [now](auto start) { return start + 60s > now; });
  const auto count = static_cast<std::size_t>(std::distance(first, starts_.end()));
  const int rateWait = count >= RequestsPerMinute ? secondsUntil(*first + 60s, now) : 0;
  return {{"requests_last_minute", count},
          {"retry_after_seconds", std::max(rateWait, secondsUntil(blockedUntil_, now))},
          {"consecutive_refusals", strikes_}};
}
} // namespace qws
