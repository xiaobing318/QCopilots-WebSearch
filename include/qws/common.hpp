#pragma once

#include <nlohmann/json.hpp>
#include <atomic>
#include <chrono>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>

namespace qws {
using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;

class Error final : public std::runtime_error {
public:
  Error(std::string value, std::string safeMessage, int httpStatus = 400,
        bool canRetry = false)
      : std::runtime_error(std::move(safeMessage)), code(std::move(value)),
        status(httpStatus), retryable(canRetry) {}
  const std::string code;
  const int status;
  const bool retryable;
};

struct CancelToken {
  std::atomic_bool cancelled{false};
  Clock::time_point deadline = Clock::now() + std::chrono::seconds(20);
  bool stopped() const {
    return cancelled.load(std::memory_order_relaxed) || Clock::now() >= deadline;
  }
};

inline Json errorPayload(const Error &error) {
  return {{"error", {{"code", error.code}, {"message", error.what()},
                     {"retryable", error.retryable}}}};
}
} // namespace qws
